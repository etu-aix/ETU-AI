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


if __name__ == "__main__":
    unittest.main()
