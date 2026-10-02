"""Parcours de profils sur l'API locale, isolé des données de l'utilisateur."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import api_server
from storage.db import StorageManager


class TestProfilesAPI(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.storage = StorageManager(Path(self.directory.name) / "profiles.db")
        self.storage_patch = patch.object(api_server, "storage", self.storage)
        self.storage_patch.start()
        self.client = TestClient(api_server.app)

    def tearDown(self):
        self.client.close()
        self.storage_patch.stop()
        self.directory.cleanup()

    def test_create_edit_activate_and_delete_profile(self):
        created = self.client.post("/api/profiles", json={
            "name": "Tuteur Python", "avatar": "🐍", "instructions": "Explique chaque étape."
        })
        self.assertEqual(created.status_code, 200)
        profile_id = created.json()["id"]
        self.assertEqual(self.client.post(f"/api/profiles/{profile_id}/activate").status_code, 200)
        conversation = self.client.post("/api/conversations", json={"title": "Variables"}).json()
        self.assertEqual(self.storage.get_conversation(conversation["conversation_id"])["profile_id"], profile_id)
        edited = self.client.put(f"/api/profiles/{profile_id}", json={
            "name": "Tuteur Python", "avatar": "🐍", "instructions": "Donne un exemple."
        })
        self.assertEqual(edited.json()["instructions"], "Donne un exemple.")
        self.assertEqual(self.client.delete(f"/api/profiles/{profile_id}").status_code, 200)
        self.assertEqual(self.storage.get_conversation(conversation["conversation_id"])["profile_id"], "default")
        self.assertEqual(self.client.get("/api/profiles").json()["active_profile_id"], "default")

    def test_rejects_unknown_profile_and_uninstalled_model(self):
        response = self.client.post("/api/conversations", json={"profile_id": "unknown"})
        self.assertEqual(response.status_code, 404)
        with patch.object(api_server.downloader, "list_installed_models", return_value=[]):
            response = self.client.post("/api/models/select", json={"model": "absent.gguf"})
        self.assertEqual(response.status_code, 404)

    def test_invalid_download_path_is_rejected_before_network(self):
        response = self.client.post("/api/models/download", json={
            "repo": "owner/model", "filename": "../outside.gguf"
        })
        self.assertEqual(response.status_code, 422)

    def test_profile_image_must_be_small_webp_data(self):
        response = self.client.post("/api/profiles", json={
            "name": "Image", "avatar": "data:image/svg+xml;base64,PHN2Zz4=", "instructions": ""
        })
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
