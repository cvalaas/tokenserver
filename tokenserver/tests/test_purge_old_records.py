# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import os
import re
import threading
import unittest
import mock
from wsgiref.simple_server import make_server

import tokenlib
import hawkauthlib
import pyramid.testing

from mozsvc.config import load_into_settings

from tokenserver.assignment import INodeAssignment
from tokenserver.scripts.purge_old_records import purge_old_records


class TestPurgeOldRecordsScript(unittest.TestCase):
    """A testcase for proper functioning of the purge_old_records.py script.

    This is a tricky one, because we have to actually run the script and
    test that it does the right thing.  We also run a mock downstream service
    so we can test that data-deletion requests go through ok.
    """

    @classmethod
    def setUpClass(cls):
        cls.service_requests = []
        cls.service_node = "http://localhost:8002"
        cls.service = make_server("localhost", 8002, cls._service_app)
        target = cls.service.serve_forever
        cls.service_thread = threading.Thread(target=target)
        # Note: If the following `start` causes the test thread to hang,
        # you may need to specify
        # `[app::pyramid.app] pyramid.worker_class = sync` in the test_*.ini
        # files
        cls.service_thread.start()
        # This silences nuisance on-by-default logging output.
        cls.service.RequestHandlerClass.log_request = lambda *a: None

    def setUp(self):
        super(TestPurgeOldRecordsScript, self).setUp()

        # Make a stub tokenserver app in-line.
        self.config = pyramid.testing.setUp()
        self.ini_file = os.path.join(os.path.dirname(__file__), 'test_sql.ini')
        settings = {}
        load_into_settings(self.ini_file, settings)
        self.config.add_settings(settings)
        self.config.include("tokenserver")

        # Configure the node-assignment backend to talk to our test service.
        self.backend = self.config.registry.getUtility(INodeAssignment)
        self.backend.add_service("sync-1.1", "{node}/1.1/{uid}")
        self.backend.add_node("sync-1.1", self.service_node, 100)

    def tearDown(self):
        if self.backend._engine.driver == 'pysqlite':
            filename = self.backend.sqluri.split('sqlite://')[-1]
            if os.path.exists(filename):
                os.remove(filename)
        else:
            self.backend._safe_execute('delete from services')
            self.backend._safe_execute('delete from nodes')
            self.backend._safe_execute('delete from users')
        pyramid.testing.tearDown()
        del self.service_requests[:]

    @classmethod
    def tearDownClass(cls):
        cls.service.shutdown()
        cls.service_thread.join()

    @classmethod
    def _service_app(cls, environ, start_response):
        cls.service_requests.append(environ)
        start_response("200 OK", [])
        return ""

    def test_purging_of_old_user_records(self):
        # Make some old user records.
        service = "sync-1.1"
        email = "test@mozilla.com"
        user = self.backend.allocate_user(service, email, client_state="aa",
                                          generation=123)
        self.backend.update_user(service, user, client_state="bb",
                                 generation=456, keys_changed_at=450)
        self.backend.update_user(service, user, client_state="cc",
                                 generation=789)
        user_records = list(self.backend.get_user_records(service, email))
        self.assertEqual(len(user_records), 3)
        user = self.backend.get_user(service, email)
        self.assertEquals(user["client_state"], "cc")
        self.assertEquals(len(user["old_client_states"]), 2)
        mock_settings = mock.Mock()
        mock_settings.dryrun = False

        # The default grace-period should prevent any cleanup.
        self.assertTrue(purge_old_records(
            self.ini_file, settings=mock_settings))
        user_records = list(self.backend.get_user_records(service, email))
        self.assertEqual(len(user_records), 3)
        self.assertEqual(len(self.service_requests), 0)

        # With no grace period, we should cleanup two old records.
        self.assertTrue(
            purge_old_records(
                self.ini_file, grace_period=0, settings=mock_settings))
        user_records = list(self.backend.get_user_records(service, email))
        self.assertEqual(len(user_records), 1)
        self.assertEqual(len(self.service_requests), 2)

        # Check that the proper delete requests were made to the service.
        secrets = self.config.registry.settings["tokenserver.secrets"]
        node_secret = secrets.get(self.service_node)[-1]
        expected_kids = ["0000000000450-uw", "0000000000123-qg"]
        for i, environ in enumerate(self.service_requests):
            # They must be to the correct path.
            self.assertEquals(environ["REQUEST_METHOD"], "DELETE")
            self.assertTrue(re.match("/1.1/[0-9]+", environ["PATH_INFO"]))
            # They must have a correct request signature.
            token = hawkauthlib.get_id(environ)
            secret = tokenlib.get_derived_secret(token, secret=node_secret)
            self.assertTrue(hawkauthlib.check_signature(environ, secret))
            userdata = tokenlib.parse_token(token, secret=node_secret)
            self.assertTrue("uid" in userdata)
            self.assertTrue("node" in userdata)
            self.assertEqual(userdata["fxa_uid"], "test")
            self.assertEqual(userdata["fxa_kid"], expected_kids[i])

        # Check that the user's current state is unaffected
        user = self.backend.get_user(service, email)
        self.assertEquals(user["client_state"], "cc")
        self.assertEquals(len(user["old_client_states"]), 0)

    def test_purging_is_not_done_on_downed_nodes(self):
        # Make some old user records.
        service = "sync-1.1"
        email = "test@mozilla.com"
        user = self.backend.allocate_user(service, email, client_state="aa")
        self.backend.update_user(service, user, client_state="bb")
        user_records = list(self.backend.get_user_records(service, email))
        self.assertEqual(len(user_records), 2)
        mock_settings = mock.Mock()
        mock_settings.dryrun = False
        mock_settings.force = False

        # With the node down, we should not purge any records.
        self.backend.update_node(service, self.service_node, downed=1)
        self.assertTrue(purge_old_records(
            self.ini_file, grace_period=0, settings=mock_settings))
        user_records = list(self.backend.get_user_records(service, email))

        self.assertEqual(len(user_records), 2)
        self.assertEqual(len(self.service_requests), 0)
        # With the node back up, we should purge correctly.
        self.backend.update_node(service, self.service_node, downed=0)
        self.assertTrue(purge_old_records(
            self.ini_file, grace_period=0, settings=mock_settings))
        user_records = list(self.backend.get_user_records(service, email))
        self.assertEqual(len(user_records), 1)
        self.assertEqual(len(self.service_requests), 1)
