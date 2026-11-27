"""Port configuration and collision handling for MAIA-Beacon."""
import json
import errno
from pathlib import Path
from tempfile import TemporaryDirectory
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest import TestCase
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
import api_server
import beacon_address
from setup_wizard import SetupWizardManager, probe_beacon_port
from storage.db import StorageManager


class BeaconPortTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name)
        (self.path / 'config.json').write_text('{"beacon_port":11343}', encoding='utf-8')
        self.storage = StorageManager(self.path / 'test.db')
        self.wizard = SetupWizardManager(storage=self.storage)
        self.patches = [patch.object(api_server, 'AI_SERVER_DIR', self.path), patch.object(beacon_address, 'CONFIG_PATH', self.path / 'config.json'), patch.object(api_server, 'storage', self.storage), patch.object(api_server, 'wizard_manager', self.wizard)]
        for item in self.patches: item.start()
        self.client = TestClient(api_server.app)
        self.headers = {'Origin': 'http://127.0.0.1:8900'}

    def tearDown(self):
        self.client.close()
        for item in reversed(self.patches): item.stop()
        self.temp.cleanup()

    def test_port_is_saved_and_used_by_consumers(self):
        from integration.ephemeral import selected_model
        from chat.sse_handler import ChatStreamHandler
        with patch.object(api_server, 'probe_beacon_port', return_value={'state':'free', 'port':12567}), patch.object(api_server.wizard_manager, 'beacon_process', None):
            response = self.client.put('/api/settings/beacon/port', json={'port':12567}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(beacon_address.beacon_url(), 'http://127.0.0.1:12567')
        self.assertEqual(selected_model(MagicMock(get_setting=lambda key, default=None: default))['base_url'], 'http://127.0.0.1:12567/v1')
        self.assertEqual(ChatStreamHandler(storage=MagicMock(), tool_executor=MagicMock()).llama_url, 'http://127.0.0.1:12567')
        self.assertEqual(json.loads((self.path / 'config.json').read_text())['beacon_port'], 12567)

    def test_bad_or_occupied_port_does_not_change_configuration(self):
        self.assertEqual(self.client.put('/api/settings/beacon/port', json={'port':65536}, headers=self.headers).status_code, 422)
        with patch.object(api_server, 'probe_beacon_port', return_value={'state':'occupied', 'port':12345}), patch.object(api_server.wizard_manager, 'beacon_process', None):
            response = self.client.put('/api/settings/beacon/port', json={'port':12345}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(beacon_address.beacon_port(), 11343)

    def test_managed_process_must_be_stopped_before_port_change(self):
        process = MagicMock(); process.poll.return_value = None
        with patch.object(api_server.wizard_manager, 'beacon_process', process):
            response = self.client.put('/api/settings/beacon/port', json={'port':12567}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertIn('Éteignez', response.json()['detail'])

    def test_probe_distinguishes_free_compatible_and_other_service(self):
        with patch('setup_wizard.socket.socket') as socket_factory:
            socket_factory.return_value.bind.return_value = None
            self.assertEqual(probe_beacon_port(1234)['state'], 'free')
        with patch('setup_wizard.socket.socket') as socket_factory, patch('setup_wizard.urllib.request.urlopen') as urlopen:
            socket_factory.return_value.bind.side_effect = OSError(errno.EADDRINUSE, 'occupied')
            urlopen.return_value.__enter__.return_value.read.return_value = b'{"type":"beacon","status":"running","active_model":"model"}'
            self.assertEqual(probe_beacon_port(1234)['state'], 'beacon')
            urlopen.return_value.__enter__.return_value.read.return_value = b'{"unrelated":true}'
            self.assertEqual(probe_beacon_port(1234)['state'], 'occupied')

    def test_attach_existing_beacon_never_stops_or_launches_it(self):
        with patch.object(api_server, 'probe_beacon_port', return_value={'state':'beacon', 'port':11343}):
            response = self.client.put('/api/settings/beacon/port', json={'port':11343, 'use_existing':True}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(self.wizard.is_external_beacon())
        with patch.object(self.wizard, 'is_beacon_online', return_value=False), patch('setup_wizard.subprocess.Popen') as popen, patch('setup_wizard.os.kill') as kill:
            self.assertFalse(self.wizard.ensure_beacon_running())
            self.assertTrue(self.wizard.stop_beacon_listener())
            popen.assert_not_called()
            kill.assert_not_called()

    def test_user_can_select_beacon_already_managed_by_etu_ai(self):
        process = MagicMock(); process.poll.return_value = None
        with patch.object(api_server, 'probe_beacon_port', return_value={'state':'beacon', 'port':11343}), \
             patch.object(self.wizard, 'beacon_process', process):
            response = self.client.put('/api/settings/beacon/port', json={'port':11343, 'use_existing':True}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(self.wizard.is_external_beacon())
        self.assertTrue(self.wizard.is_beacon_enabled())

    def test_disappearing_beacon_is_not_attached(self):
        with patch.object(api_server, 'probe_beacon_port', return_value={'state':'free', 'port':11343}):
            response = self.client.put('/api/settings/beacon/port', json={'port':11343, 'use_existing':True}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.wizard.is_external_beacon())

    def test_socket_failure_is_not_claimed_as_available(self):
        with patch('setup_wizard.socket.socket') as socket_factory:
            socket_factory.return_value.bind.side_effect = OSError(errno.EACCES, 'denied')
            self.assertEqual(probe_beacon_port(1234)['state'], 'unknown')

    def test_discovery_checks_all_local_tcp_listeners(self):
        netstat = MagicMock(stdout=(
            '  TCP    0.0.0.0:11343  0.0.0.0:0 LISTENING 10\n'
            '  TCP    [::]:54000  [::]:0 LISTENING 20\n'
        ))
        def probe(port, timeout):
            return {'port': port, 'state': 'beacon', 'active_model': None} if port == 54000 else {'port': port, 'state': 'occupied'}
        with patch('setup_wizard.platform.system', return_value='Windows'), \
             patch('setup_wizard.subprocess.run', return_value=netstat), \
             patch('setup_wizard.probe_beacon_port', side_effect=probe):
            from setup_wizard import discover_beacon_ports
            self.assertEqual(discover_beacon_ports(), [{'port': 54000, 'state': 'beacon', 'active_model': None}])

    def test_detects_real_local_http_service(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"type":"beacon","status":"idle","active_model":null}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = probe_beacon_port(server.server_port)
            self.assertEqual(result["state"], "beacon")
            self.assertIsNone(result["active_model"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_existing_instance_on_another_port_is_recorded(self):
        with patch.object(api_server, 'probe_beacon_port', return_value={'state':'beacon', 'port':12567}):
            response = self.client.put('/api/settings/beacon/port', json={'port':12567, 'use_existing':True}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(beacon_address.beacon_port(), 12567)
        self.assertTrue(self.wizard.is_external_beacon())
        self.assertTrue(self.wizard.is_beacon_enabled())
        with patch.object(self.wizard, 'is_beacon_online', return_value=True):
            response = self.client.post('/api/settings/beacon/restart', headers=self.headers)
        self.assertEqual(response.status_code, 409)

    def test_setup_finishes_on_chosen_port_without_download(self):
        model_dir = self.path / "models"
        model_dir.mkdir()
        (model_dir / "model.gguf").write_bytes(b"x" * (1024 * 1024 + 1))
        self.wizard.state_mgr = MagicMock()
        process = MagicMock()
        process.stdout = []
        process.wait.return_value = 0
        process.poll.return_value = None
        (self.path / "config.json").write_text('{"beacon_port":12567}', encoding="utf-8")
        with patch('setup_wizard.AI_SERVER_DIR', self.path), \
             patch('setup_wizard.probe_beacon_port', return_value={'state':'free', 'port':12567}), \
             patch.object(self.wizard, '_start_tracked_process', return_value=process) as start, \
             patch.object(self.wizard, 'is_model_ready', return_value=True):
            self.wizard._run_pipeline([{'repo':'local', 'filename':'model.gguf'}])
        self.assertEqual(self.wizard.status, 'completed')
        self.assertEqual(start.call_count, 2)
        self.assertTrue(self.storage.get_setting('setup_completed'))
        self.assertEqual(self.wizard.state_mgr.update_state.call_args.args[0]['installed_components']['beacon']['port'], 12567)

    def test_setup_rejects_existing_listener_before_starting_processes(self):
        model_dir = self.path / "models"
        model_dir.mkdir()
        (model_dir / "model.gguf").write_bytes(b"model")
        self.wizard.state_mgr = MagicMock()
        with patch('setup_wizard.AI_SERVER_DIR', self.path), \
             patch('setup_wizard.probe_beacon_port', return_value={'state':'beacon', 'port':11343}), \
             patch.object(self.wizard, '_start_tracked_process') as start:
            self.wizard._run_pipeline([{'repo':'local', 'filename':'model.gguf'}])
        self.assertEqual(self.wizard.status, 'error')
        self.assertIn('déjà occupé', self.wizard.error_message)
        start.assert_not_called()

