import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from companion import Companion, DEFAULT_SHORTCUT, SHORTCUTS, watch_server


class CompanionTests(unittest.TestCase):
    def test_toggle_and_close_keep_window_and_position(self):
        window, storage = Mock(), Mock()
        companion = Companion(window, storage)
        companion.toggle()
        window.show.assert_called_once()
        companion.moved(-240, 120)
        storage.set_setting.assert_called_once_with("companion_position", {"x": -240, "y": 120})
        companion.toggle()
        window.hide.assert_called_once()
        companion.toggle()
        self.assertFalse(companion.close())
        self.assertFalse(companion.visible)
        self.assertEqual(window.hide.call_count, 2)
        window.destroy.assert_not_called()

    def test_default_shortcut_is_supported(self):
        self.assertIn(DEFAULT_SHORTCUT, SHORTCUTS)

    def test_server_restart_reloads_the_companion(self):
        companion = Mock()
        responses = [
            nullcontext(SimpleNamespace(status=200)),
            nullcontext(SimpleNamespace(read=lambda: b'{"pid": 100}')),
            nullcontext(SimpleNamespace(status=200)),
            nullcontext(SimpleNamespace(read=lambda: b'{"pid": 101}')),
        ]
        with patch("companion.time.sleep", side_effect=[None, None, StopIteration]), \
             patch("companion.urllib.request.urlopen", side_effect=responses):
            with self.assertRaises(StopIteration):
                watch_server(companion)
        companion.window.load_url.assert_called_once_with("http://127.0.0.1:8900/?companion=1")
        companion.window.destroy.assert_not_called()

    def test_shortcut_endpoint_validates_before_persisting(self):
        from fastapi import HTTPException
        import api_server

        storage = Mock()
        with patch.object(api_server, "require_local_admin"), patch.object(api_server, "storage", storage):
            result = api_server.set_companion_settings(Mock(), api_server.CompanionShortcutRequest(shortcut="ctrl-alt-e"))
            self.assertEqual(result["shortcut"], "ctrl-alt-e")
            storage.set_setting.assert_called_once_with("companion_shortcut", "ctrl-alt-e")
            with self.assertRaises(HTTPException):
                api_server.set_companion_settings(Mock(), api_server.CompanionShortcutRequest(shortcut="invalid"))
            self.assertEqual(storage.set_setting.call_count, 1)
