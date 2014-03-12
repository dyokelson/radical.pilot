"""API layer tests
"""

import os
import sys
import sagapilot
import unittest

import uuid
from copy import deepcopy
from sagapilot.db import Session
from pymongo import MongoClient

# DBURL defines the MongoDB server URL and has the format mongodb://host:port.
# For the installation of a MongoDB server, refer to the MongoDB website:
# http://docs.mongodb.org/manual/installation/
DBURL = os.getenv("SAGAPILOT_DBURL")
if DBURL is None:
    print "ERROR: SAGAPILOT_DBURL (MongoDB server URL) is not defined."
    sys.exit(1)
    
DBNAME = 'sagapilot_unittests'

#-----------------------------------------------------------------------------
#
class Test_Session(unittest.TestCase):
    # silence deprecation warnings under py3

    def setUp(self):
        # clean up fragments from previous tests
        client = MongoClient(DBURL)
        client.drop_database(DBNAME)

    def tearDown(self):
        # clean up after ourselves 
        client = MongoClient(DBURL)
        client.drop_database(DBNAME)

    def failUnless(self, expr):
        # St00pid speling.
        return self.assertTrue(expr)

    def failIf(self, expr):
        # St00pid speling.
        return self.assertFalse(expr)

    #-------------------------------------------------------------------------
    #
    def test__session_create(self):
        """ Tests if creating a new session works as epxected.
        """
        for _ in range(1, 4):
            session = sagapilot.Session(database_url=DBURL, database_name=DBNAME)
            
        client = MongoClient(DBURL)
        collections = client[DBNAME].collection_names()
        assert len(collections) == 4, "Wrong number of sessions in database"

        session.destroy()

    #-------------------------------------------------------------------------
    #
    def test__session_reconnect(self):
        """ Tests if reconnecting to an existing session works as epxected.
        """
        session_ids = []
        for _ in range(1, 4):
            session = sagapilot.Session(database_url=DBURL, database_name=DBNAME)
            session_ids.append(session.uid)

        for sid in session_ids:
            session_r = sagapilot.Session(database_url=DBURL, session_uid=sid, database_name=DBNAME)
            assert session_r.uid == sid, "Session IDs don't match"

        session.destroy()

    #-------------------------------------------------------------------------
    #
    def test__credentials_reconnect(self):
        """ Tests if reconnecting to an existing session works as epxected and if
        credentials are reloaded properly.
        """
        session = sagapilot.Session(database_url=DBURL, database_name=DBNAME)

        # Add an ssh identity to the session.
        cred1 = sagapilot.SSHCredential()
        cred1.user_id = "tg802352"
        session.add_credential(cred1)

        # Add an ssh identity to the session.
        cred2 = sagapilot.SSHCredential()
        cred2.user_id = "abcedesds"
        session.add_credential(cred2)

        assert len(session.credentials) == 2

        session2 = sagapilot.Session(database_url=DBURL, session_uid=session.uid)
        print "Session: {0} ".format(session2)

        assert len(session2.credentials) == 2

        for cred in session2.credentials:
            assert cred.as_dict() in [cred1.as_dict(), cred2.as_dict()]

        session.destroy()


        
