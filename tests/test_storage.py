"""Tests unitaires pour la persistance locale SQLite."""
import unittest
from pathlib import Path
import tempfile
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.db import StorageManager


class TestStorageManager(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_chats.db"
        self.storage = StorageManager(db_path=self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_conversation_lifecycle(self):
        # Création
        conv_id = self.storage.create_conversation("Discussion Test", "ministral-3b")
        self.assertIsNotNone(conv_id)

        # Récupération
        conv = self.storage.get_conversation(conv_id)
        self.assertEqual(conv["title"], "Discussion Test")
        self.assertEqual(conv["model_used"], "ministral-3b")

        # Mise à jour titre
        updated = self.storage.update_conversation_title(conv_id, "Titre Modifié")
        self.assertTrue(updated)
        self.assertEqual(self.storage.get_conversation(conv_id)["title"], "Titre Modifié")

        # Liste
        convs = self.storage.list_conversations()
        self.assertEqual(len(convs), 1)

        # Suppression
        deleted = self.storage.delete_conversation(conv_id)
        self.assertTrue(deleted)
        self.assertIsNone(self.storage.get_conversation(conv_id))

    def test_messages(self):
        conv_id = self.storage.create_conversation("Test Messages")
        msg_id1 = self.storage.add_message(conv_id, "user", "Bonjour !")
        msg_id2 = self.storage.add_message(conv_id, "assistant", "<p class='etu-text'>Salut !</p>")

        messages = self.storage.get_messages(conv_id)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["content"], "Bonjour !")
        self.assertEqual(messages[1]["role"], "assistant")

    def test_settings(self):
        self.storage.set_setting("pairing_token", "secret_token_123")
        token = self.storage.get_setting("pairing_token")
        self.assertEqual(token, "secret_token_123")

        self.storage.set_setting("dict_config", {"theme": "dark", "volume": 100})
        cfg = self.storage.get_setting("dict_config")
        self.assertEqual(cfg["theme"], "dark")

    def test_models_cache(self):
        self.storage.register_model(
            filename="model.gguf",
            repo="org/model-gguf",
            size_bytes=1024000,
            sha256="abc123def456",
            backend="cuda",
        )
        cached = self.storage.list_cached_models()
        self.assertEqual(len(cached), 1)
        self.assertEqual(cached[0]["filename"], "model.gguf")


class TestStateManager(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temp_dir.name) / "test_app_state.json"
        from storage.state_manager import StateManager
        # Reset singleton instance for isolated test
        StateManager._instance = None
        self.sm = StateManager(file_path=self.state_file)

    def tearDown(self):
        from storage.state_manager import StateManager
        StateManager._instance = None
        self.temp_dir.cleanup()

    def test_state_lifecycle(self):
        self.assertFalse(self.sm.is_setup_completed())
        state = self.sm.get_state()
        self.assertIn("setup", state)
        self.assertIn("installed_components", state)

        self.sm.record_installed_model("test.gguf", "repo/test", size_bytes=2048)
        self.sm.mark_setup_completed(primary_model="test.gguf")

        self.assertTrue(self.sm.is_setup_completed())
        updated_state = self.sm.get_state()
        self.assertTrue(updated_state["setup"]["completed"])
        self.assertEqual(updated_state["active_model"], "test.gguf")
        self.assertEqual(len(updated_state["installed_models"]), 1)

        self.sm.reset_setup()
        self.assertFalse(self.sm.is_setup_completed())


if __name__ == "__main__":
    unittest.main()
