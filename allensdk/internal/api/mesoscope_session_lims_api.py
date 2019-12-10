import pandas as pd
import json
import os
from typing import List

from allensdk.internal.core.lims_utilities import safe_system_path
from allensdk.internal.api import PostgresQueryMixin
from allensdk.brain_observatory.behavior.sync import get_sync_data
import logging

logger = logging.getLogger(__name__)
logging.basicConfig(format='%(levelname)s:%(message)s', level=logging.DEBUG)


class MesoscopeSessionLimsApi(PostgresQueryMixin):
    """
    class to access database information for mesoscope session
    to use as api in mesoscope.session
    """

    def __init__(self, session_id, do_run=True):
        """
        Class to access session-level Mesoscope data in LIMS. Features unique to mesoscope: splitting of the timestamps,
        queries to access pairing of the planes, while it is missing from the DB
        -----
        :param session_id: same as session is in LIMS
        """
        self.session_id = session_id
        self.experiment_ids = None
        self.pairs = None
        self.splitting_json = None
        self.session_folder = None
        self.session_df = None
        self.sync_path = None
        super().__init__()
        if do_run:
            self.get_session_df()
            self.get_session_folder()
            self.get_paired_experiments()
            self.get_splitting_json()
            self.get_sync_file()

    def get_well_known_file(self, file_type) -> pd.DataFrame:
        """Gets a well_known_file's location"""
        query = ' '.join(['SELECT wkf.storage_directory, wkf.filename FROM well_known_files wkf',
                          'JOIN well_known_file_types wkft',
                          'ON wkf.well_known_file_type_id = wkft.id',
                          'WHERE',
                          'attachable_id = {}'.format(self.session_id),
                          'AND wkft.name = \'{}\''.format(file_type)])

        query = query.format(self.session_id)
        filepath = pd.read_sql(query, self.get_connection())
        return filepath

    def get_session_experiments(self) -> pd.DataFrame:
        """experiments in this session"""
        query = ' '.join((
            "SELECT oe.id as experiment_id",
            "FROM ophys_experiments oe",
            "WHERE oe.ophys_session_id = {}"
        ))
        return pd.read_sql(query.format(self.session_id), self.get_connection())


    def get_session_df(self) -> pd.DataFrame:
        """Dataframe on session information"""
        query = ' '.join(("SELECT oe.id as experiment_id, os.id as session_id, os.name as ses_name"
                          ", os.storage_directory as session_folder, oe.storage_directory as experiment_folder",
                          ", sp.name as specimen",
                          ", os.date_of_acquisition as date",
                          ", oe.calculated_depth as depth",
                          ", st.acronym as structure",
                          ", os.parent_session_id as parent_id",
                          ", oe.workflow_state as wfl_state ",
                          ", users.login as operator "
                          "FROM ophys_experiments oe",
                          "join ophys_sessions os on os.id = oe.ophys_session_id "
                          "join specimens sp on sp.id = os.specimen_id "
                          "join projects p on p.id = os.project_id "
                          "join structures st on st.id = oe.targeted_structure_id "
                          "join users on users.id = os.operator_id"
                          " WHERE os.id='{}' ",
                          ))
        query = query.format(self.session_id)
        self.session_df = pd.read_sql(query, self.get_connection())
        return self.session_df

    def get_session_folder(self) -> str:
        """lims path to session folder from sessions_df"""
        _session = pd.DataFrame(self.get_session_df())
        session_folder = _session['session_folder']
        self.session_folder = safe_system_path(session_folder.values[0])
        return self.session_folder

    def get_splitting_json(self) -> str:
        # this is only necessary while information on planes pairs is not yet added to lims
        """returns path to session's splitting json"""
        session_folder = self.get_session_folder()
        json_path = os.path.join(session_folder, f"MESOSCOPE_FILE_SPLITTING_QUEUE_{self.session_id}_input.json")
        self.splitting_json = safe_system_path(json_path)
        if not os.path.isfile(self.splitting_json):
            logger.error("Unable to find splitting json for session: {}".format(self.session_id))
        return self.splitting_json

    def get_paired_experiments(self) -> List[List[int]]:
        """returns list of pairs (list of ints) for given session"""
        splitting_json = self.get_splitting_json()
        self.pairs = []
        with open(splitting_json, "r") as f:
            data = json.load(f)
        for pg in data.get("plane_groups", []):
            self.pairs.append([p["experiment_id"] for p in pg.get("ophys_experiments", [])])
        return self.pairs

    def get_sync_file(self) -> str:
        """lims path to sync file for given session"""
        sync_file_df = self.get_well_known_file(file_type='OphysRigSync')
        sync_file_dir = safe_system_path(sync_file_df['storage_directory'].values[0])
        sync_file_name = sync_file_df['filename'].values[0]
        return os.path.join(sync_file_dir, sync_file_name)


if __name__ == "__main__":
    test_session_id = 992201455
    ms = MesoscopeSessionLimsApi(test_session_id)
    print(f'Session ID: {ms.session_id}')
    print(f'Experiments in session: {ms.get_session_experiments()}')
    print(f'Session folder: {ms.get_session_folder()}')
    print(f'Session data frame: {ms.get_session_df()}')
    print(f'Session splitting json: {ms.get_splitting_json()}')
    print(f'Session pairs: {ms.get_paired_experiments()}')
    print(f'Session sync file: {ms.get_sync_file()}')
    #print(f'Session timestamps, split: {ms.split_session_timestamps()}')
