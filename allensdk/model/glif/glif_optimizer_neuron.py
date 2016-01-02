import logging
import numpy as np
import scipy.interpolate as spi

import allensdk.model.glif.glif_neuron as glif_neuron

# TODO: license
# TODO: document

class GlifNeuronException( Exception ):
    """ Exception for catching simulation errors and reporting intermediate data. """
    def __init__(self, message, data):
        super(Exception, self).__init__(message)
        self.data = data

class GlifBadInitializationException( Exception ):
    """ Exception raised when voltage is above threshold at the beginning of a sweep. i.e. probably caused by the optimizer. """
    def __init__(self, message, dv, step):
        super(Exception, self).__init__(message)
        self.dv = dv
        self.step=step

class GlifOptimizerNeuron( glif_neuron.GlifNeuron ):
    '''Contains methods for running the neuron model in a "forced-spike" paradigm
    used during optimization.
    '''
    
    TYPE = "GLIF"
    
    def __init__(self, *args, **kwargs):
        
        super(GlifOptimizerNeuron, self).__init__(*args, **kwargs)
        
        self.extrapolation_method_name = kwargs.get('extrapolation_method_name', 'endpoints')
        if self.extrapolation_method_name == 'endpoints':
            self.extrapolation_method = extrapolate_model_spike_from_endpoints
        elif self.extrapolation_method_name == 'endpoints_single_tau':
            self.extrapolation_method = extrapolate_model_spike_from_endpoints_single_tau
        else:
            raise Exception('unknown extrapolation method: %s' % self.extrapolation_method_name)
        
        #TODO: what is this where is it comming from?
        self.dt_multiplier = kwargs.get('dt_multiplier', 1)
            
    @classmethod
    def from_dict(cls, d):
        
        return cls(El = d['El'],
                   dt = d['dt'],
#                   tau = d['tau'],
                   asc_tau_array=d['asc_tau_array'],
                   R_input = d['R_input'],
                   C = d['C'],
                   asc_amp_array = d['asc_amp_array'],
                   spike_cut_length = d['spike_cut_length'],
                   th_inf = d['th_inf'],
                   th_adapt=d['th_adapt'],
                   coeffs = d.get('coeffs', {}),
                   AScurrent_dynamics_method = d['AScurrent_dynamics_method'],
                   voltage_dynamics_method = d['voltage_dynamics_method'],
                   threshold_dynamics_method = d['threshold_dynamics_method'],
                   voltage_reset_method = d['voltage_reset_method'],
                   AScurrent_reset_method = d['AScurrent_reset_method'],
                   threshold_reset_method = d['threshold_reset_method'],
                   init_method_data = d.get('init_method_data', {}),
                   init_voltage = d['init_voltage'],
                   init_threshold = d['init_threshold'],
                   init_AScurrents = d['init_AScurrents'],
                   extrapolation_method_name = d.get('extrapolation_method_name', 'endpoints'),
                   dt_multiplier = d.get('dt_multiplier',1)
                   )
        
    def to_dict(self):
        
        curr_dict = super(GlifOptimizerNeuron, self).to_dict()
        curr_dict.update({'extrapolation_method_name':self.extrapolation_method_name,
                          'dt_multiplier':self.dt_multiplier})

        return curr_dict

    def run_with_biological_spikes(self, stimulus, response, bio_spike_time_steps):
        """ Run the neuron simulation over a stimulus, but do not allow the model to spike on its own.  Rather,
        force the simulation to spike and reset at a given set of spike indices.  Dynamics rules are applied
        between spikes regardless of the simulated voltage and threshold values.  Reset rules are applied only 
        at input spike times. This is used during optimization to force the model to follow the spikes of biological data.
        The model is optimized in this way so that history effects due to spiking can be adequately modeled.  For example, 
        every time the model spikes a new set of afterspike currents will be initiated. To ensure that afterspike currents 
        can be optimized, we force them to be initiated at the time of the biological spike.

        Parameters
        ----------
        stimulus : np.ndarray
            vector of scalar current values
        respones : np.ndarray
            vector of scalar voltage values
        bio_spike_time_steps : list
            spike time step indices

        Returns
        -------
        dict
            a dictionary containing:
                'voltage': simulated voltage values,
                'threshold': simulated threshold values,
                'AScurrent_matrix': afterspike currents during the simulation,
                'grid_model_spike_times': spike times of the model aligned to the simulation grid (when it would have spiked),
                'interpolated_model_spike_times': spike times of the model linearly interpolated between time steps,
                'grid_ISI': interspike interval between grid model spike times,
                'interpolated_ISI': interspike interval between interpolated model spike times,
                'grid_bio_spike_model_voltage': voltage of the model at biological/input spike times,
                'grid_bio_spike_model_threshold': voltage of the model at biological/input spike times interpolated between time steps
        """
        
        self.reset_method_data() #get rid of lingering method data

        voltage_t0 = self.init_voltage
        threshold_t0 = self.init_threshold
        AScurrents_t0 = self.init_AScurrents
        
        if voltage_t0>threshold_t0:
            raise GlifBadInitializationException("Voltage STARTS above threshold: voltage_t0 (%f) threshold_t0 (%f)" % ( voltage_t0, threshold_t0, voltage_t0 - threshold_t0, 10000000.0))

        start_index = 0
        end_index = 0
            
        try:
            num_spikes = len(bio_spike_time_steps)
            
            # if there are no target spikes, just run until the model spikes
            if num_spikes == 0:  

                start_index = 0
                end_index = len(stimulus)

                # evaluate the model starting from the beginning until the model spikes
                run_data = self.run_until_biological_spike(voltage_t0, threshold_t0, AScurrents_t0, 
                                                           stimulus, response, start_index, end_index, 
                                                           []) 

                voltage = run_data['voltage']
                threshold = run_data['threshold']
                AScurrent_matrix = run_data['AScurrent_matrix']

                if len(voltage) != len(stimulus):
                    logging.warning('Your voltage output is not the same length as your stimulus')
                if len(threshold) != len(stimulus):
                    logging.warning('Your threshold output is not the same length as your stimulus')
                if len(AScurrent_matrix) != len(stimulus):
                    logging.warning('Your AScurrent_matrix output is not the same length as your stimulus')
                                          
                # do not keep track of the spikes in the model that spike if the target doesn't spike.
                grid_ISI = np.array([])
                interpolated_ISI = np.array([])

                grid_model_spike_times = np.array([]) 
                interpolated_model_spike_times = np.array([])
                
                grid_model_spike_voltages = np.array([]) 
                interpolated_model_spike_voltages = np.array([]) 

                grid_bio_spike_model_voltage = np.array([])
                grid_bio_spike_model_threshold = np.array([])
            else:
                # initialize the output arrays
                grid_ISI = np.empty(num_spikes)
                interpolated_ISI = np.empty(num_spikes)

                grid_model_spike_times = np.empty(num_spikes)
                interpolated_model_spike_times = np.empty(num_spikes)
                
                grid_model_spike_voltages = np.empty(num_spikes) 
                interpolated_model_spike_voltages = np.empty(num_spikes)

                grid_bio_spike_model_voltage = np.empty(num_spikes)
                grid_bio_spike_model_threshold = np.empty(num_spikes)

                spikeIndStart = 0
                
                voltage = np.empty(len(stimulus))
                voltage[:] = np.nan  
                threshold = np.empty(len(stimulus))
                threshold[:] = np.nan
                AScurrent_matrix = np.empty(shape=(len(stimulus), len(AScurrents_t0)))
                AScurrent_matrix[:] = np.nan
        
                # run the simulation over the interspike intervals (starting at the beginning of the simulation). 
                start_index = 0
                for spike_num in range(num_spikes):

                    if spike_num % 10 == 0:
                        logging.debug("spike %d / %d" % (spike_num,  num_spikes))

                    end_index = int(bio_spike_time_steps[spike_num])
    
                    assert start_index < end_index, Exception("start_index > end_index: this is probably because spike_cut_length is longer than the previous inter-spike interval")

                    # run the simulation over this interspike interval
#                     t0 = time.time()
                    run_data = self.run_until_biological_spike(voltage_t0, threshold_t0, AScurrents_t0, 
                                                               stimulus, response, start_index, end_index, 
                                                               bio_spike_time_steps)
#                     print 'fast', time.time() - t0
                    
#                     curr_voltage = run_data_fast['voltage']
#                     curr_threshold = run_data_fast['threshold']
#                     voltage_scrubbed = curr_voltage[np.logical_not(np.isnan(curr_voltage))]
#                     threshold_scrubbed = curr_threshold[np.logical_not(np.isnan(curr_threshold))]  
#                     
#                     tmp_t = np.linspace(0,1,len(voltage_scrubbed))
#                     print voltage_scrubbed
#                     plt.plot(tmp_t, voltage_scrubbed)
#                     plt.plot(tmp_t, threshold_scrubbed)
#                     
#                     plt.show()
#                     sys.exit()

#                     for key, val in run_data.items():
#                         print key, val
#                     sys.exit()

                    
                    # assign the simulated data to the correct locations in the output arrays
                    voltage[start_index:end_index] = run_data['voltage']
                    threshold[start_index:end_index] = run_data['threshold']
                    AScurrent_matrix[start_index:end_index,:] = run_data['AScurrent_matrix']

                    grid_ISI[spike_num] = run_data['grid_model_spike_time']
                    interpolated_ISI[spike_num] = run_data['interpolated_model_spike_time']

                    grid_model_spike_times[spike_num] = run_data['grid_model_spike_time'] + start_index * self.dt 
                    interpolated_model_spike_times[spike_num] = run_data['interpolated_model_spike_time'] + start_index * self.dt
                    
                    grid_model_spike_voltages[spike_num] = run_data['grid_model_spike_voltage']
                    interpolated_model_spike_voltages[spike_num] = run_data['interpolated_model_spike_voltage'] 

                    grid_bio_spike_model_voltage[spike_num] = run_data['grid_bio_spike_model_voltage']
                    grid_bio_spike_model_threshold[spike_num] = run_data['grid_bio_spike_model_threshold']

                    # update the voltage, threshold, and afterspike currents for the next interval
                    voltage_t0 = run_data['voltage_t0']
                    threshold_t0 = run_data['threshold_t0']
                    AScurrents_t0 = run_data['AScurrents_t0']
                    
                    start_index = end_index

                    # if cutting spikes, jump forward the appropriate amount of time
                    if self.spike_cut_length > 0:
                        start_index += self.spike_cut_length
                
                # simulate the portion of the stimulus between the last spike and the end of the array.
                # no spikes are recorded from this time!
                run_data = self.run_until_biological_spike(voltage_t0, threshold_t0, AScurrents_t0, 
                                                           stimulus, response, start_index, len(stimulus),
                                                           bio_spike_time_steps) 

                voltage[start_index:] = run_data['voltage']
                threshold[start_index:] = run_data['threshold']
                AScurrent_matrix[start_index:,:] = run_data['AScurrent_matrix']

            # make sure that the output data has the correct number of spikes in it
            if ( len(interpolated_model_spike_times) != num_spikes or 
                 len(grid_model_spike_times) != num_spikes or 
                 len(grid_ISI) != num_spikes or 
                 len(interpolated_ISI) != num_spikes or 
                 len(grid_bio_spike_model_voltage) != num_spikes or 
                 len(grid_bio_spike_model_threshold) != num_spikes):
                raise Exception('The number of spikes in your output does not match your target')

        except GlifNeuronException, e:
            
            # if an exception was raised during run_until_spike, record any simulated data before exiting
            voltage[start_index:end_index] = e.data['voltage']
            threshold[start_index:end_index] = e.data['threshold']
            AScurrent_matrix[start_index:end_index,:] = e.data['AScurrent_matrix']

            out = {
                'voltage': voltage,
                'threshold': threshold,
                'AScurrent_matrix': AScurrent_matrix,

                'grid_ISI': grid_ISI,
                'interpolated_ISI': interpolated_ISI,

                'grid_model_spike_times': grid_model_spike_times,
                'interpolated_model_spike_times': interpolated_model_spike_times,
                
                'grid_model_spike_voltages': grid_model_spike_voltages,
                'interpolated_model_spike_voltages': interpolated_model_spike_voltages,

                'grid_bio_spike_model_voltage': grid_bio_spike_model_voltage,
                'grid_bio_spike_model_threshold': grid_bio_spike_model_threshold 
                }

            raise GlifNeuronException(e.message, out)

        return {
            'voltage': voltage,
            'threshold': threshold,
            'AScurrent_matrix': AScurrent_matrix,

            'grid_model_spike_times': grid_model_spike_times,
            'interpolated_model_spike_times': interpolated_model_spike_times,
            
            'grid_model_spike_voltages': grid_model_spike_voltages,
            'interpolated_model_spike_voltages': interpolated_model_spike_voltages,

            'grid_ISI': grid_ISI,
            'interpolated_ISI': interpolated_ISI,

            'grid_bio_spike_model_voltage': grid_bio_spike_model_voltage,
            'grid_bio_spike_model_threshold': grid_bio_spike_model_threshold
        }
                
#     def run_until_biological_spike(self, voltage_t0, threshold_t0, AScurrents_t0, 
#                                    stimulus, response, start_index, end_index, 
#                                    bio_spike_time_steps):
#         """ Run the neuron simulation over a segment of a stimulus given initial conditions. The model simulates
#         until either the model spikes or the end of the segment is reached.  If the model does not spike, a 
#         spike time is extrapolated past the end of the simulation segment.
# 
#         Parameters
#         ----------
#         voltage_t0 : float
#             the current voltage of the neuron
#         threshold_t0 : float
#             the current spike threshold level of the neuron
#         AScurrents_t0 : np.ndarray
#             the current state of the afterspike currents in the neuron
#         stimulus : np.ndarray
#             the full stimulus array (not just the segment of data being simulated)
#         response : np.ndarray
#             the full response array (not just the segment of data being simulated)
#         start_index : int
#             index to start simulating
#         end_index : int
#             index *after* the last index to be simulated
#         bio_spike_time_steps : list
#             time steps of input spikes
# 
#         Returns
#         -------
#         dict
#             a dictionary containing:
#                 'voltage': simulated voltage value
#                 'threshold': simulated threshold values
#                 'AScurrent_matrix': afterspike current values during the simulation
#                 'grid_model_spike_time': model spike time (in units of dt) 
#                 'interpolated_model_spike_time': model spike time (in units of dt) interpolated between time steps
#                 'voltage_t0': reset voltage value to be used in subsequent simulation interval 
#                 'threshold_t0': reset threshold value to be used in subsequent simulation interval
#                 'AScurrents_t0': reset afterspike current value to be used in subsequent simulation interval
#                 'grid_bio_spike_model_voltage': model voltage at the time of the input spike
#                 'grid_bio_spike_model_threshold': model threshold at the time of the input spike
#         """
#         
# 
# 
#         # preallocate arrays and matricies
#         num_time_steps = end_index - start_index
#         num_spikes = len(bio_spike_time_steps)
# 
#         voltage_out = np.empty(num_time_steps)
#         voltage_out[:] = np.nan
#         threshold_out = np.empty(num_time_steps)
#         threshold_out[:] = np.nan
#         AScurrent_matrix = np.empty(shape=(num_time_steps, len(AScurrents_t0)))
#         AScurrent_matrix[:] = np.nan
# 
#         grid_model_spike_time = None
#         grid_model_spike_voltage = None
#         
#         interpolated_model_spike_time = None
#         interpolated_model_spike_voltage = None
#         
#         # calculate the model values between the two target spikes (don't stop if there is a spike)
#         for time_step in xrange(num_time_steps):
# 
#             # Note that here you are not recording the first v0 because that was recoded at the end of the previous spike
#             voltage_out[time_step] = voltage_t0 
#             threshold_out[time_step] = threshold_t0
#             AScurrent_matrix[time_step,:] = np.matrix(AScurrents_t0) 
#             
#             if np.isnan(voltage_t0) or np.isinf(voltage_t0) or np.isnan(threshold_t0) or np.isinf(threshold_t0) or any(np.isnan(AScurrents_t0)) or any(np.isinf(AScurrents_t0)):
#                 logging.error(self)
#                 logging.error('time step: %d / %d' % (time_step, num_time_steps))
#                 logging.error('    voltage_t0: %f' % voltage_t0)
#                 logging.error('    voltage started the run at: %f' % voltage_out[0])
#                 logging.error('    voltage before: %s' % voltage_out[time_step-20:time_step])
#                 logging.error('    threshold_t0: %f' % threshold_t0)
#                 logging.error('    threshold started the run at: %f' % threshold_out[0])
#                 logging.error('    threshold before: %s' % threshold_out[time_step-20:time_step])
#                 logging.error('    AScurrents_t0: %s' % AScurrents_t0)
#                 raise GlifNeuronException('Invalid threshold, voltage, or after-spike current encountered.', {
#                         'voltage': voltage_out,
#                         'threshold': threshold_out,
#                         'AScurrent_matrix': AScurrent_matrix
#                         })
# 
#             (voltage_t1, threshold_t1, AScurrents_t1) = self.dynamics(voltage_t0, threshold_t0, AScurrents_t0, stimulus[time_step+start_index], time_step+start_index, bio_spike_time_steps) #TODO fix list versus array
# 
#             
#             voltage_t0=voltage_t1
#             threshold_t0=threshold_t1
#             AScurrents_t0=AScurrents_t1
#         
#         # comment this out if you just want TRD
#         
#         #--see if model crosses threshold in the interval (TRDX)
#         grid_model_spike_time, grid_model_spike_voltage, interpolated_model_spike_time, interpolated_model_spike_voltage = find_first_model_spike(voltage_out, threshold_out, voltage_t1, threshold_t1, self.dt)
# 
#         # if the model never spiked, extrapolate to guess when it would have spiked
#         if grid_model_spike_time is None: 
#             grid_model_spike_time, grid_model_spike_voltage, interpolated_model_spike_time, interpolated_model_spike_voltage = extrapolate_model_spike_from_endpoints(voltage_out, threshold_out, voltage_t1, threshold_t1, self.dt, self.tau_m)
#         
#         # if the target spiked, reset so that next round will start at reset but not recording it in the voltage here.
#         # note that at the last section of the stimulus where there is no current injected the model will be reset even if
#         # the biological neuron doesn't spike.  However, this doesnt matter as it won't be recorded. 
#         if num_spikes > 0:
#             if end_index<len(stimulus):
#                 # BEGIN HACK
#                 bio_v = response[end_index]
#                 bio_thresh = bio_v
#                 
#                 # overwrite the slow component of threshold with biological spike threshold
#                 if self.threshold_reset_method.name == 'adapt_sum_slow_fast':                
#                     self.update_method_data['th_voltage'][-1] = bio_v 
#                                 
#                 voltage_t1 = bio_v
#                 threshold_t1 = bio_thresh
#                 # END HACK
#             
#                 (voltage_t0, threshold_t0, AScurrents_t0) = self.reset(voltage_t1, threshold_t1, AScurrents_t1)
#             else:
#                 (voltage_t0, threshold_t0, AScurrents_t0) = None, None, None
#         
#         return {
#             'voltage': voltage_out, 
#             'threshold': threshold_out, 
#             'AScurrent_matrix': AScurrent_matrix, 
# 
#             'grid_model_spike_time': grid_model_spike_time,
#             'interpolated_model_spike_time': interpolated_model_spike_time,
#             
#             'grid_model_spike_voltage': grid_model_spike_voltage,
#             'interpolated_model_spike_voltage': interpolated_model_spike_voltage,
# 
#             'voltage_t0': voltage_t0, 
#             'threshold_t0': threshold_t0, 
#             'AScurrents_t0': AScurrents_t0, 
# 
#             'grid_bio_spike_model_voltage': voltage_t1, 
#             'grid_bio_spike_model_threshold': threshold_t1            
#             }
        
    def run_until_biological_spike(self, voltage_t0, threshold_t0, AScurrents_t0, 
                                   stimulus, response, start_index, end_index, 
                                   bio_spike_time_steps):
        """ Run the neuron simulation over a segment of a stimulus given initial conditions. The model simulates
        until either the model spikes or the end of the segment is reached.  If the model does not spike, a 
        spike time is extrapolated past the end of the simulation segment.

        Parameters
        ----------
        voltage_t0 : float
            the current voltage of the neuron
        threshold_t0 : float
            the current spike threshold level of the neuron
        AScurrents_t0 : np.ndarray
            the current state of the afterspike currents in the neuron
        stimulus : np.ndarray
            the full stimulus array (not just the segment of data being simulated)
        response : np.ndarray
            the full response array (not just the segment of data being simulated)
        start_index : int
            index to start simulating
        end_index : int
            index *after* the last index to be simulated
        bio_spike_time_steps : list
            time steps of input spikes

        Returns
        -------
        dict
            a dictionary containing:
                'voltage': simulated voltage value
                'threshold': simulated threshold values
                'AScurrent_matrix': afterspike current values during the simulation
                'grid_model_spike_time': model spike time (in units of dt) 
                'interpolated_model_spike_time': model spike time (in units of dt) interpolated between time steps
                'voltage_t0': reset voltage value to be used in subsequent simulation interval 
                'threshold_t0': reset threshold value to be used in subsequent simulation interval
                'AScurrents_t0': reset afterspike current value to be used in subsequent simulation interval
                'grid_bio_spike_model_voltage': model voltage at the time of the input spike
                'grid_bio_spike_model_threshold': model threshold at the time of the input spike
        """
        
        grid_model_spike_time = None
        grid_model_spike_voltage = None
        interpolated_model_spike_time = None
        interpolated_model_spike_voltage = None

        # preallocate arrays and matricies
        num_time_steps_fine = end_index - start_index
        t_fine_grid = np.arange(num_time_steps_fine)*self.dt

        # Apply refinement factor:
        dt_old = self.dt
        self.dt =  self.dt*self.dt_multiplier
        
        local_coarse_indicies=np.append(np.arange(num_time_steps_fine)[::self.dt_multiplier], end_index - start_index)
        if len(local_coarse_indicies)==2:
            pass
        
        num_time_steps_coarse = len(local_coarse_indicies)
        voltage_out_coarse_grid = np.empty(num_time_steps_coarse)
        voltage_out_coarse_grid[:] = np.nan
        threshold_out_coarse_grid = np.empty(num_time_steps_coarse)
        threshold_out_coarse_grid[:] = np.nan
        AScurrent_matrix_coarse_grid = np.empty(shape=(num_time_steps_coarse, len(AScurrents_t0))) 
        AScurrent_matrix_coarse_grid[:] = np.nan
        #these grid times are in the local frame of reference
        t_coarse_grid = np.arange(num_time_steps_coarse-1)*self.dt #subtracting the one off here because appending the actual last time that is not the same dt.
        t_coarse_grid = np.append(t_coarse_grid, t_fine_grid[-1])
        dt_vector=t_coarse_grid[1:]-t_coarse_grid[:-1]

        global_coarse_indicies=local_coarse_indicies+start_index
        stimulus_coarse=[stimulus[global_coarse_indicies[ii]:global_coarse_indicies[ii+1]].mean() for ii in range(len(global_coarse_indicies)-1)]

        #why am I looking over time step as opposed to matrix; fine but how do I take into account last epic
        for time_step in range(len(local_coarse_indicies)-1):  #TODO: check this may not record the last value
            # Note that here you are not recording the first v0 because that was recoded at the end of the previous spike
            voltage_out_coarse_grid[time_step] = voltage_t0 
            threshold_out_coarse_grid[time_step] = threshold_t0
            AScurrent_matrix_coarse_grid[time_step,:] = np.matrix(AScurrents_t0) 
            
            
            
            if np.isnan(voltage_t0) or np.isinf(voltage_t0) or np.isnan(threshold_t0) or np.isinf(threshold_t0) or any(np.isnan(AScurrents_t0)) or any(np.isinf(AScurrents_t0)):
                logging.error(self)
                logging.error('time step: %d / %d' % (time_step, num_time_steps_coarse))
                logging.error('    voltage_t0: %f' % voltage_t0)
                logging.error('    voltage started the run at: %f' % voltage_out_coarse_grid[0])
                logging.error('    voltage before: %s' % voltage_out_coarse_grid[time_step-20:time_step])
                logging.error('    threshold_t0: %f' % threshold_t0)
                logging.error('    threshold started the run at: %f' % threshold_out_coarse_grid[0])
                logging.error('    threshold before: %s' % threshold_out_coarse_grid[time_step-20:time_step])
                logging.error('    AScurrents_t0: %s' % AScurrents_t0)
                if self.init_method_data['a_spike']:
                    logging.error('    a_spike: %s' % self.init_method_data['a_spike'])
                if self.init_method_data['b_spike']:
                    logging.error('    b_spike: %s' % self.init_method_data['b_spike'])
                #need to ouput the correct matrix of values for plotting
                #set up output to be the original matrix size of nan's
                
                temp_fine_grid_for_intp=np.arange(0,t_coarse_grid[time_step-1], dt_old)
                voltage_out_fine_grid = np.empty(num_time_steps_fine)
                voltage_out_fine_grid[:] = np.nan
                threshold_out_fine_grid = np.empty(num_time_steps_fine)
                threshold_out_fine_grid[:] = np.nan

                fv = spi.interp1d(t_coarse_grid[:time_step], voltage_out_coarse_grid[:time_step], assume_sorted=True, bounds_error=False, fill_value=voltage_out_coarse_grid[-1])
                ft = spi.interp1d(t_coarse_grid[:time_step], threshold_out_coarse_grid[:time_step], assume_sorted=True, bounds_error=False, fill_value=threshold_out_coarse_grid[-1])

                voltage_with_error = fv(temp_fine_grid_for_intp)
                threshold_with_error = ft(temp_fine_grid_for_intp)
                voltage_out_fine_grid[:len(voltage_with_error)]=voltage_with_error
                threshold_out_fine_grid[:len(threshold_with_error)]=threshold_with_error
                
                AScurrent_matrix = np.empty(shape=(num_time_steps_fine, len(AScurrents_t0)))
                AScurrent_matrix[:] = np.nan
                for ii in range(len(AScurrents_t0)):
                    curr_fASc = spi.interp1d(t_coarse_grid[:time_step], AScurrent_matrix_coarse_grid[:time_step,ii], assume_sorted=True, bounds_error=False, fill_value=AScurrent_matrix_coarse_grid[-1,ii])
                    temp_asc=curr_fASc(temp_fine_grid_for_intp)
                    AScurrent_matrix[:len(temp_asc),ii] = temp_asc 
                
                raise GlifNeuronException('Invalid threshold, voltage, or after-spike current encountered.', {                                                                         
                        'voltage': voltage_out_fine_grid,
                        'threshold': threshold_out_fine_grid,
                        'AScurrent_matrix': AScurrent_matrix
                        })
#             print voltage_t0, threshold_t0, AScurrents_t0, stimulus[time_step+start_index], time_step+start_index, bio_spike_time_steps
#            why is this function being provided bio spike times?
#            Should pass in dt here because it might change for last step in euler exact
            
            if time_step == len(local_coarse_indicies)-1:
                pass
            #!!!!!!!!!!!!!!!!!!!!!!TODO: THIS IS A HUGE HACK!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
            #Changing dt be the dt of the bin (which is variable for the last bin)
            self.dt=dt_vector[time_step]
            (voltage_t1, threshold_t1, AScurrents_t1) = self.dynamics(voltage_t0, threshold_t0, AScurrents_t0, stimulus_coarse[time_step], time_step+start_index, bio_spike_time_steps) #TODO fix list versus array

            voltage_t0=voltage_t1
            threshold_t0=threshold_t1
            AScurrents_t0=AScurrents_t1
            
        # Inserting the last values into the nan at the end of the matricies so that when do the interpolation the end of the vector will not be nans
        # Note this should not mess with any of the outputs because is the interploated values that are the output.
        voltage_out_coarse_grid[time_step+1] = voltage_t0 
        threshold_out_coarse_grid[time_step+1] = threshold_t0
        AScurrent_matrix_coarse_grid[time_step+1,:] = np.matrix(AScurrents_t0)

        
        # Reset dt to previous value:
        self.dt =  dt_old
                
#        plt.plot(t_coarse_grid, voltage_out_coarse_grid, label='v course grid')
#        plt.plot(t_coarse_grid, threshold_out_coarse_grid, label='thr course grid')
        
        fv = spi.interp1d(t_coarse_grid, voltage_out_coarse_grid, assume_sorted=True, bounds_error=False, fill_value=voltage_out_coarse_grid[-1])
        ft = spi.interp1d(t_coarse_grid, threshold_out_coarse_grid, assume_sorted=True, bounds_error=False, fill_value=threshold_out_coarse_grid[-1])
#        plt.plot(t_fine_grid, fv(t_fine_grid), '--', label='v fine grid')
#        plt.plot(t_fine_grid, ft(t_fine_grid), '--', label='thr fine grid')
#        plt.legend(loc=2)
#         
#        plt.show()
        
#         voltage_out_fine_grid = np.empty(num_time_steps_fine)
#         voltage_out_fine_grid[:] = np.nan
#         threshold_out_fine_grid = np.empty(num_time_steps_fine)
#         threshold_out_fine_grid[:] = np.nan
  
        voltage_out = fv(t_fine_grid)
        threshold_out = ft(t_fine_grid)
        AScurrent_matrix = np.empty(shape=(num_time_steps_fine, len(AScurrents_t0)))
        AScurrent_matrix[:] = np.nan
        for ii in range(len(AScurrents_t0)):
            curr_fASc = spi.interp1d(t_coarse_grid, AScurrent_matrix_coarse_grid[:,ii], assume_sorted=True, bounds_error=False, fill_value=AScurrent_matrix_coarse_grid[-1,ii])
            AScurrent_matrix[:,ii] = curr_fASc(t_fine_grid) 
        
        # comment this out if you just want TRD
        
        #
        #-------USE THIS FUNCTION IF YOU WANT TO GET MODEL CROSSING THRESHOLD------- 
        grid_model_spike_time, grid_model_spike_voltage, interpolated_model_spike_time, interpolated_model_spike_voltage = find_first_model_spike(voltage_out, threshold_out, voltage_t1, threshold_t1, self.dt)

        # if the model never spiked, extrapolate to guess when it would have spiked
        if grid_model_spike_time is None: 
            
            grid_model_spike_time, grid_model_spike_voltage, interpolated_model_spike_time, interpolated_model_spike_voltage = self.extrapolation_method(self, voltage_out, threshold_out, voltage_t1, threshold_t1, self.dt)
        
        # if the target spiked, reset so that next round will start at reset but not recording it in the voltage here.
        # note that at the last section of the stimulus where there is no current injected the model will be reset even if
        # the biological neuron doesn't spike.  However, this doesnt matter as it won't be recorded. 
        num_spikes = len(bio_spike_time_steps)
        if num_spikes > 0:
            if end_index<len(stimulus):
                # BEGIN HACK ( input to threshold reset rule is biological spike voltage for adapt_sum_slow_fast threshold reset method
                #bio_v = response[end_index]
                #bio_thresh = bio_v
                # 
                # overwrite the slow component of threshold with biological spike threshold
                #if self.threshold_reset_method.name == 'adapt_sum_slow_fast':                
                #    self.update_method_data['th_voltage'][-1] = bio_v 
                #                
                #voltage_t1 = bio_v
                #threshold_t1 = bio_thresh
                # END HACK
                
                if self.threshold_reset_method.name == 'adapt_sum_slow_fast':
                    voltage_t1 = threshold_t1
                #---------------------------------------------------------------------------------------------------------------------
                #---------below is the option you choose for reseting based on interpolated voltage versus voltage at----------------- 
                #---------the time of the biological spike----------------------------------------------------------------------------
                #---------------------------------------------------------------------------------------------------------------------
                #(voltage_t0, threshold_t0, AScurrents_t0) = self.reset(interpolated_model_spike_voltage, interpolated_model_spike_voltage, AScurrents_t1) #USE THIS IF YOU WANT TO USE USE MODEL VALUES AT MODEL SPIKE
                (voltage_t0, threshold_t0, AScurrents_t0, bad_reset_flag) = self.reset(voltage_t1, threshold_t1, AScurrents_t1)  #USE THIS IF YOU WANT TO USE MODEL VALUES AT TIME OF BIOLOGICAL SPIKE
                #---------------------------------------------------------------------------------------------------------------------
            else:
                (voltage_t0, threshold_t0, AScurrents_t0) = None, None, None
        
        return {
            'voltage': voltage_out, 
            'threshold': threshold_out, 
            'AScurrent_matrix': AScurrent_matrix, 

            'grid_model_spike_time': grid_model_spike_time,
            'interpolated_model_spike_time': interpolated_model_spike_time,
            
            'grid_model_spike_voltage': grid_model_spike_voltage,
            'interpolated_model_spike_voltage': interpolated_model_spike_voltage,

            'voltage_t0': voltage_t0, 
            'threshold_t0': threshold_t0, 
            'AScurrents_t0': AScurrents_t0, 

            'grid_bio_spike_model_voltage': voltage_t1, 
            'grid_bio_spike_model_threshold': threshold_t1            
            }

def find_first_model_spike(voltage, threshold, voltage_t1, threshold_t1, dt):
    num_time_steps = len(voltage)

    for time_step in xrange(num_time_steps): 
        if voltage[time_step] > threshold[time_step]:
            grid_model_spike_time = dt * (time_step-1)
            grid_model_spike_voltage = voltage[time_step-1]
            
            interpolated_model_spike_time = glif_neuron.interpolate_spike_time(dt, time_step-1, 
                                                                               threshold[time_step-1], threshold[time_step], 
                                                                               voltage[time_step-1], voltage[time_step])
            
            interpolated_model_spike_voltage = interpolate_spike_voltage(dt, time_step-1, 
                                                                         threshold[time_step-1], threshold[time_step], 
                                                                         voltage[time_step-1], voltage[time_step])
                
            return grid_model_spike_time, grid_model_spike_voltage, interpolated_model_spike_time, interpolated_model_spike_voltage

    # if the last voltage is above threshold and there hasn't already been a spike
    if voltage_t1 > threshold_t1: 
        grid_model_spike_time = dt * ( num_time_steps - 1 )
        grid_model_spike_voltage = voltage_t1
            
        interpolated_model_spike_time = glif_neuron.interpolate_spike_time(dt, num_time_steps - 1, threshold[num_time_steps-1], threshold_t1, voltage[num_time_steps-1], voltage_t1)
        interpolated_model_spike_voltage = interpolate_spike_voltage(dt, num_time_steps, threshold[-1], threshold_t1, voltage[-1], voltage_t1)

        return grid_model_spike_time, grid_model_spike_voltage, interpolated_model_spike_time, interpolated_model_spike_voltage


    return None, None, None, None

def extrapolate_model_spike_from_endpoints(neuron, voltage, threshold, voltage_t1, threshold_t1, dt):
    
    #--extrapolate using first point in ISI and last point in ISI
    num_time_steps = len(voltage)
    
    interpolated_model_spike_time = extrapolate_spike_time(dt, num_time_steps, threshold[0], threshold_t1, voltage[0], voltage_t1)
    interpolated_model_spike_voltage = extrapolate_spike_voltage(dt, num_time_steps, threshold[0], threshold_t1, voltage[0], voltage_t1)
            
    grid_model_spike_time = np.ceil(interpolated_model_spike_time / dt) * dt  # grid spike time based off extrapolated spike time
    grid_model_spike_voltage = interpolated_model_spike_voltage

    result = grid_model_spike_time, grid_model_spike_voltage, interpolated_model_spike_time, interpolated_model_spike_voltage
    

    return result

    

def extrapolate_model_spike_from_endpoints_single_tau(neuron, voltage, threshold, voltage_t1, threshold_t1, dt):
    tau_m = neuron.tau_m
    num_time_steps = len(voltage)
    ii = np.floor(tau_m/dt)
    starting_ind = max(0,(num_time_steps - ii)) 
    result = extrapolate_model_spike_from_endpoints(neuron, voltage[starting_ind:], threshold[starting_ind:], voltage_t1, threshold_t1, dt)
    
    return result
    
def extrapolate_spike_time(dt, num_time_steps, threshold_t0, threshold_t1, voltage_t0, voltage_t1):
    """ Given two voltage and threshold values and an interval between them, extrapolate a spike time
    by intersecting lines the thresholds and voltages. """
    return glif_neuron.line_crossing_x(dt * num_time_steps, voltage_t0, voltage_t1, threshold_t0, threshold_t1)

def extrapolate_spike_voltage(dt, num_time_steps, threshold_t0, threshold_t1, voltage_t0, voltage_t1):
    """ Given two voltage and threshold values and an interval between them, extrapolate a spike time
    by intersecting lines the thresholds and voltages. """
    return glif_neuron.line_crossing_y(dt * num_time_steps, voltage_t0, voltage_t1, threshold_t0, threshold_t1)
    
def interpolate_spike_voltage(dt, time_step, threshold_t0, threshold_t1, voltage_t0, voltage_t1):
    """ Given two voltage and threshold values, the dt between them and the initial time step, interpolate
    a spike time within the dt interval by intersecting the two lines. """
    return time_step*dt + glif_neuron.line_crossing_y(dt, voltage_t0, voltage_t1, threshold_t0, threshold_t1)
