"""Tests d'intégration des endpoints FastAPI de l'application ETU-AI."""
import os
import io
import json
import unittest
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from api_server import app
import api_server
from storage.db import StorageManager
from storage.state_manager import StateManager
from setup_wizard import SetupWizardManager


class TestAPIServer(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = StorageManager(Path(self.temp_dir.name) / "api.db")
        self.original_state_instance = StateManager._instance
        StateManager._instance = None
        self.state = StateManager(Path(self.temp_dir.name) / "state.json")
        self.wizard = SetupWizardManager(storage=self.storage)
        self.wizard.state_mgr = self.state
        self.patches = [
            patch.object(api_server, "storage", self.storage),
            patch.object(api_server, "state_manager", self.state),
            patch.object(api_server, "wizard_manager", self.wizard),
            patch.object(api_server.stream_handler, "storage", self.storage),
        ]
        for item in self.patches:
            item.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        for item in reversed(self.patches):
            item.stop()
        StateManager._instance = self.original_state_instance
        self.temp_dir.cleanup()

    def test_status_endpoint(self):
        resp = self.client.get("/api/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "online")
        self.assertEqual(data["app_name"], "ETU-AI")
        self.assertIn("backend", data)
        self.assertIn("paired_with_etu", data)
        self.assertIn("model_state", data)

    def test_app_update_endpoint_reports_release_and_persists_user_choice(self):
        installer = {"name": "ETU-AI-Setup-0.9.1.exe", "url": "https://github.com/etu-aix/ETU-AI/releases/download/v0.9.1/ETU-AI-Setup-0.9.1.exe", "size": 1024, "digest": None}
        release = {"status": "available", "version": "v0.9.1", "url": "https://github.com/etu-aix/ETU-AI/releases/latest", "assets": [installer]}
        with patch.object(api_server, "latest_app_release", return_value=release), \
             patch.object(api_server, "installed_version", return_value="v0.9.0"), \
             patch.object(api_server, "is_newer_version", return_value=True), \
             patch.object(api_server.platform, "system", return_value="Windows"):
            response = self.client.get("/api/updates/app", headers={"Origin": "http://127.0.0.1:8900"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["version"], "v0.9.1")
        self.assertEqual(response.json()["installed_version"], "v0.9.0")
        self.assertTrue(response.json()["update_available"])
        self.assertTrue(response.json()["installer_available"])
        self.assertFalse(response.json()["direct_install_supported"])
        self.assertEqual(response.json()["installer"]["url"], installer["url"])
        self.assertEqual(response.json()["choice"], "")

        choice = self.client.put("/api/updates/app/choice", json={"version": "v0.9.1", "choice": "later"},
                                 headers={"Origin": "http://127.0.0.1:8900"})
        self.assertEqual(choice.status_code, 200)
        with patch.object(api_server, "latest_app_release", return_value=release), \
             patch.object(api_server, "installed_version", return_value="v0.9.0"), \
             patch.object(api_server, "is_newer_version", return_value=True), \
             patch.object(api_server.platform, "system", return_value="Windows"):
            self.assertEqual(self.client.get("/api/updates/app", headers={"Origin": "http://127.0.0.1:8900"}).json()["choice"], "later")

        with patch.object(api_server, "latest_app_release", return_value=release), \
             patch.object(api_server, "installed_version", return_value="v0.9.0"), \
             patch.object(api_server, "is_newer_version", return_value=True), \
             patch.object(api_server.platform, "system", return_value="Linux"):
            linux = self.client.get("/api/updates/app", headers={"Origin": "http://127.0.0.1:8900"}).json()
        self.assertTrue(linux["update_available"])
        self.assertFalse(linux["installer_available"])
        self.assertIsNone(linux["installer"])

    def test_app_installer_download_uses_latest_platform_asset_and_disposes_temp_file(self):
        import hashlib
        import tempfile

        payload = b"verified fake installer"
        installer = {"name": "ETU-AI-Setup-0.9.1.exe", "url": "https://github.com/etu-aix/ETU-AI/releases/download/v0.9.1/ETU-AI-Setup-0.9.1.exe", "size": len(payload), "digest": "sha256:" + hashlib.sha256(payload).hexdigest()}
        release = {"status": "available", "version": "v0.9.1", "url": "https://github.com/etu-aix/ETU-AI/releases/latest", "assets": [installer]}
        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.object(api_server, "latest_app_release", return_value=release), \
             patch.object(api_server, "download_verified_app_installer") as download, \
             patch.object(api_server.platform, "system", return_value="Windows"):
            package = Path(temp_dir) / installer["name"]
            package.write_bytes(payload)
            download.return_value = package
            response = self.client.get("/api/updates/app/download/v0.9.1", headers={"Origin": "http://127.0.0.1:8900"})
            self.assertFalse(package.exists())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, payload)
        self.assertIn("ETU-AI-Setup-0.9.1.exe", response.headers["content-disposition"])
        download.assert_called_once_with(installer, "v0.9.1")

    def test_direct_update_actions_reject_non_local_origins(self):
        for method, url in (
            (self.client.post, "/api/updates/app/prepare/v1.2.0"),
            (self.client.get, "/api/updates/app/progress"),
            (self.client.post, "/api/updates/app/install/v1.2.0"),
        ):
            with self.subTest(url=url):
                response = method(url, headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"})
                self.assertEqual(response.status_code, 403)

    def test_direct_update_requires_the_bundled_windows_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / "python" / "python.exe"
            python.parent.mkdir()
            python.touch()
            with patch.object(api_server, "BASE_DIR", root), \
                 patch.object(api_server.sys, "executable", str(python)):
                self.assertFalse(api_server._packaged_windows_install())
                (root / "ETU-AI.exe").touch()
                self.assertEqual(api_server._packaged_windows_install(), os.name == "nt")
                with patch.object(api_server.sys, "executable", str(root / "other-python.exe")):
                    self.assertFalse(api_server._packaged_windows_install())

        with patch.object(api_server, "installed_version", return_value="v1.0.0"):
            response = self.client.post("/api/updates/app/prepare/v1.2.0",
                                        headers={"Origin": "http://127.0.0.1:8900"})
        self.assertEqual(response.status_code, 400)
        with patch.object(api_server, "installed_version", return_value="v1.0.0"):
            response = self.client.post("/api/updates/app/install/v1.2.0",
                                        headers={"Origin": "http://127.0.0.1:8900"})
        self.assertEqual(response.status_code, 400)

    def test_startup_setting_uses_the_bundled_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "ETU-AI.exe"
            launcher.touch()
            with patch.object(api_server, "BASE_DIR", root):
                self.assertEqual(api_server._startup_command(), api_server.subprocess.list2cmdline([str(launcher), "--mode", "local"]))

    def test_direct_update_keeps_only_a_fully_verified_installer(self):
        import hashlib
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from tools.sign_release import create_manifest

        key = Ed25519PrivateKey.generate()
        installer = {"name": "ETU-AI-Setup-1.2.0.exe",
                     "url": "https://github.com/etu-aix/ETU-AI/releases/download/v1.2.0/ETU-AI-Setup-1.2.0.exe",
                     "size": 8, "digest": "sha256:" + hashlib.sha256(b"MZsetup!").hexdigest()}
        sidecar = {"name": installer["name"] + ".manifest.json", "url": installer["url"] + ".manifest.json"}
        release = {"version": "v1.2.0", "assets": [installer, sidecar]}
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / installer["name"]
            package.write_bytes(b"MZsetup!")
            manifest = create_manifest(package, "v1.2.0", installer["url"], key)
            with patch.object(api_server, "latest_app_release", return_value=release), \
                 patch.object(api_server, "select_app_installer", return_value=installer), \
                 patch("integration.updates.urlopen", return_value=io.BytesIO(json.dumps(manifest).encode())), \
                 patch.object(api_server, "RELEASE_SIGNING_PUBLIC_KEY", key.public_key().public_bytes_raw()), \
                 patch("integration.updates.RELEASE_SIGNING_PUBLIC_KEY", key.public_key().public_bytes_raw()), \
                 patch.object(api_server, "download_verified_app_installer", return_value=package):
                api_server._app_update_state = {"status": "downloading", "version": "v1.2.0"}
                api_server._prepare_direct_app_update("v1.2.0")
            self.assertEqual(api_server._app_update_state["status"], "ready")
            self.assertIs(api_server._app_update_path, package)
            package.unlink()
            api_server._app_update_path = None
            api_server._app_update_manifest = None

    def test_direct_update_discards_a_corrupted_installer(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from tools.sign_release import create_manifest

        key = Ed25519PrivateKey.generate()
        installer = {"name": "ETU-AI-Setup-1.2.0.exe",
                     "url": "https://github.com/etu-aix/ETU-AI/releases/download/v1.2.0/ETU-AI-Setup-1.2.0.exe",
                     "size": 8}
        release = {"version": "v1.2.0", "assets": [installer]}
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / installer["name"]
            package.write_bytes(b"MZsetup!")
            manifest = create_manifest(package, "v1.2.0", installer["url"], key)
            package.write_bytes(b"MZbroken")
            with patch.object(api_server, "latest_app_release", return_value=release), \
                 patch.object(api_server, "select_app_installer", return_value=installer), \
                 patch.object(api_server, "fetch_signed_installer_manifest", return_value=manifest), \
                 patch.object(api_server, "RELEASE_SIGNING_PUBLIC_KEY", key.public_key().public_bytes_raw()), \
                 patch.object(api_server, "download_verified_app_installer", return_value=package):
                api_server._app_update_state = {"status": "downloading", "version": "v1.2.0"}
                api_server._prepare_direct_app_update("v1.2.0")
            self.assertEqual(api_server._app_update_state["status"], "error")
            self.assertFalse(package.exists())
            self.assertIsNone(api_server._app_update_path)

    @unittest.skipUnless(os.name == "nt", "Le lancement direct de l’installateur est réservé à Windows")
    def test_direct_update_supervisor_receives_the_recorded_companion_pid(self):
        import hashlib
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from tools.sign_release import create_manifest

        key = Ed25519PrivateKey.generate()
        payload = b"MZverified setup"
        version = "v1.2.0"
        installer_name = "ETU-AI-Setup-1.2.0.exe"
        url = f"https://github.com/etu-aix/ETU-AI/releases/download/{version}/{installer_name}"
        original_state = api_server._app_update_state
        original_path = api_server._app_update_path
        original_manifest = api_server._app_update_manifest
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            asset = temp / installer_name
            asset.write_bytes(payload)
            package = temp / "etu-app-installer-verified.exe"
            package.write_bytes(payload)
            manifest = create_manifest(asset, version, url, key)
            pid_file = temp / "companion.pid"
            pid_file.write_text("4242", encoding="ascii")
            api_server._app_update_state = {"status": "ready", "version": version}
            api_server._app_update_path = package
            api_server._app_update_manifest = manifest
            try:
                with patch.object(api_server, "require_local_admin"), \
                     patch.object(api_server, "installed_version", return_value="v1.0.0"), \
                     patch.object(api_server, "_packaged_windows_install", return_value=True), \
                     patch.object(api_server, "RELEASE_SIGNING_PUBLIC_KEY", key.public_key().public_bytes_raw()), \
                     patch.object(api_server, "companion_pid_file", return_value=pid_file), \
                     patch.object(api_server.tempfile, "gettempdir", return_value=directory), \
                     patch.object(api_server.subprocess, "Popen") as launch, \
                     patch.object(api_server.threading, "Thread") as end_thread:
                    response = api_server.install_direct_app_update(object(), version)

                command = launch.call_args.args[0]
                self.assertEqual(response, {"status": "installing"})
                self.assertEqual(command[8], "4242")
                self.assertEqual(command[11], str(api_server.BASE_DIR / "python" / "python.exe"))
                self.assertEqual(command[12], str(pid_file))
                end_thread.return_value.start.assert_called_once_with()
            finally:
                api_server._app_update_state = original_state
                api_server._app_update_path = original_path
                api_server._app_update_manifest = original_manifest

    def test_beacon_listener_fallback_handles_unavailable_command_output(self):
        from types import ModuleType, SimpleNamespace

        psutil_stub = ModuleType("psutil")
        psutil_stub.net_connections = lambda **kwargs: (_ for _ in ()).throw(OSError("permission denied"))

        with patch("setup_wizard.platform.system", return_value="Windows"), \
             patch.dict(sys.modules, {"psutil": psutil_stub}), \
             patch("setup_wizard.subprocess.run", return_value=SimpleNamespace(stdout=None)):
            self.assertEqual(self.wizard._beacon_listener_pids(), [])

    def test_beacon_listener_fallback_decodes_binary_netstat_output(self):
        from types import ModuleType, SimpleNamespace

        psutil_stub = ModuleType("psutil")
        psutil_stub.net_connections = lambda **kwargs: (_ for _ in ()).throw(OSError("permission denied"))
        output = b"  TCP    127.0.0.1:11343    0.0.0.0:0    LISTENING    4321\r\n"

        with patch("setup_wizard.platform.system", return_value="Windows"), \
             patch.dict(sys.modules, {"psutil": psutil_stub}), \
             patch("setup_wizard.subprocess.run", return_value=SimpleNamespace(stdout=output)):
            self.assertEqual(self.wizard._beacon_listener_pids(), [4321])

    def test_beacon_model_guard_detects_another_model_inventory(self):
        status = {"available_models": ["qwen2.5-1.5b-instruct-q4_k_m", "gemma-4-E4B-it-Q4_K_M.gguf"]}
        self.assertTrue(api_server._beacon_has_model(status, "gemma-4-E4B-it-Q4_K_M"))
        self.assertFalse(api_server._beacon_has_model(status, "Qwen3-4B-Q4_K_M"))
        self.assertTrue(api_server._beacon_has_model({}, "Qwen3-4B-Q4_K_M"))

    def test_conversations_api(self):
        # Création
        create_resp = self.client.post("/api/conversations", json={"title": "Test API Conv"})
        self.assertEqual(create_resp.status_code, 200)
        conv_id = create_resp.json()["conversation_id"]

        # Liste
        list_resp = self.client.get("/api/conversations")
        self.assertEqual(list_resp.status_code, 200)
        conv_ids = [c["id"] for c in list_resp.json()["conversations"]]
        self.assertIn(conv_id, conv_ids)

        # Messages
        msg_resp = self.client.get(f"/api/conversations/{conv_id}/messages")
        self.assertEqual(msg_resp.status_code, 200)
        self.assertEqual(len(msg_resp.json()["messages"]), 0)

        # Suppression
        del_resp = self.client.delete(f"/api/conversations/{conv_id}")
        self.assertEqual(del_resp.status_code, 200)
        self.assertTrue(del_resp.json()["success"])

    def test_models_catalog_api(self):
        resp = self.client.get("/api/models/catalog")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("catalog", data)
        self.assertGreaterEqual(len(data["catalog"]), 3)

    def test_handshake_api(self):
        resp = self.client.post(
            "/api/handshake",
            json={"pairing_token": "token_test_456", "student_id": "etu_999"},
        )
        self.assertEqual(resp.status_code, 501)

        # Statut mis à jour
        status_resp = self.client.get("/api/status")
        self.assertFalse(status_resp.json()["paired_with_etu"])

    def test_static_ui_serving(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("ETU-AI", resp.text)

    def test_setup_endpoints(self):
        # 1. Vérification du statut setup
        status_resp = self.client.get("/api/setup/status")
        self.assertEqual(status_resp.status_code, 200)
        data = status_resp.json()
        self.assertIn("completed", data)
        self.assertIn("steps", data)
        self.assertEqual(len(data["steps"]), 4)

        # 2. Reset setup
        reset_resp = self.client.post("/api/setup/reset")
        self.assertEqual(reset_resp.status_code, 200)
        self.assertTrue(reset_resp.json()["success"])

        # 3. État persistant de l'application (app_state)
        state_resp = self.client.get("/api/setup/state")
        self.assertEqual(state_resp.status_code, 200)
        state_data = state_resp.json()
        self.assertIn("installed_components", state_data)
        self.assertIn("setup", state_data)

        # 4. Mise à jour de l'état
        update_resp = self.client.post("/api/setup/state", json={"preferences": {"theme_ambient_glow": True}})
        self.assertEqual(update_resp.status_code, 200)
        self.assertTrue(update_resp.json()["preferences"]["theme_ambient_glow"])

    def test_model_selection_api(self):
        from unittest.mock import patch, MagicMock
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "model": "Ministral-3-3B-Instruct-2512-Q4_K_M"}'
        mock_resp.__enter__.return_value = mock_resp
        status_mock = MagicMock()
        status_mock.read.return_value = b'{"status": "running", "active_model": "Ministral-3-3B-Instruct-2512-Q4_K_M"}'
        status_mock.__enter__.return_value = status_mock

        def beacon_response(request, **kwargs):
            url = request if isinstance(request, str) else request.full_url
            return mock_resp if url.endswith("/api/select") else status_mock

        with patch("urllib.request.urlopen", side_effect=beacon_response), patch.object(api_server.downloader, "list_installed_models", return_value=["Ministral-3-3B-Instruct-2512-Q4_K_M.gguf"]):
            sel_resp = self.client.post("/api/models/select", json={"model": "Ministral-3-3B-Instruct-2512-Q4_K_M.gguf"})
            self.assertEqual(sel_resp.status_code, 200)
            data = sel_resp.json()
            self.assertTrue(data.get("success"))

        status_resp = self.client.get("/api/status")
        self.assertEqual(status_resp.status_code, 200)
        st_data = status_resp.json()
        self.assertIn("active_model", st_data)
        self.assertIn("ram_total_gib", st_data)

    def test_huggingface_search_api(self):
        resp = self.client.get("/api/models/huggingface/search?q=qwen&limit=5")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("models", data)
        self.assertIn("hardware", data)
        self.assertGreaterEqual(len(data["models"]), 1)
        first = data["models"][0]
        self.assertIn("repo", first)
        self.assertIn("compatibility", first)

    def test_huggingface_compatible_api(self):
        resp = self.client.get("/api/models/huggingface/compatible?limit=10")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("models", data)
        self.assertGreaterEqual(len(data["models"]), 1)
        for m in data["models"]:
            self.assertTrue(m["compatibility"]["is_compatible"])

    def test_huggingface_files_api(self):
        resp = self.client.get("/api/models/huggingface/files?repo=Qwen/Qwen2.5-1.5B-Instruct-GGUF")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["repo"], "Qwen/Qwen2.5-1.5B-Instruct-GGUF")
        self.assertIn("files", data)
        self.assertIsInstance(data["files"], list)

    def test_auto_select_api(self):
        resp = self.client.get("/api/models/auto-select?count=1")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("recommended_models", data)
        self.assertIn("primary_model", data)
        self.assertIsNotNone(data["primary_model"])
        self.assertIn("hardware", data)

    def test_auto_select_apply_api(self):
        with patch.object(api_server.downloader, "list_installed_models", return_value=[]):
            resp = self.client.post("/api/models/auto-select/apply")
        self.assertEqual(resp.status_code, 409)


if __name__ == "__main__":
    unittest.main()
