"""Tests d'intégration des endpoints FastAPI de l'application ETU-AI."""
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from api_server import app


class TestAPIServer(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(app)

    def test_status_endpoint(self):
        resp = self.client.get("/api/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "online")
        self.assertIn("backend", data)
        self.assertIn("paired_with_etu", data)

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
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])

        # Statut mis à jour
        status_resp = self.client.get("/api/status")
        self.assertTrue(status_resp.json()["paired_with_etu"])

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

        with patch("urllib.request.urlopen", return_value=mock_resp):
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
        resp = self.client.post("/api/models/auto-select/apply")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("success"))
        self.assertIn("active_model", data)


if __name__ == "__main__":
    unittest.main()
