"""Le flux local ne doit pas bloquer les autres requêtes FastAPI."""

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chat.sse_handler import ChatStreamHandler
from storage.db import StorageManager
from mock_sse import mock_sse


DELAYED_LINES = [
    b'data: {"choices":[{"delta":{"content":"Bon"}}]}\n',
    b'data: {"choices":[{"delta":{"content":"jour"}}]}\n',
    b'data: [DONE]\n',
]


class TestStreaming(unittest.IsolatedAsyncioTestCase):
    async def test_remote_provider_identity_is_in_the_system_context(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = StorageManager(Path(directory) / "chat.db")
            conversation = storage.create_conversation("Limites API")
            storage.set_setting("api_providers", {
                "active_provider": "mistral", "configs": {
                    "mistral": {"base_url": "https://api.mistral.ai/v1", "model": "ministral-test"}
                }
            })
            with patch("chat.sse_handler.get_api_key", return_value="test-key"), mock_sse() as requests:
                async for _ in ChatStreamHandler(storage=storage).stream_chat(conversation, "Y a-t-il des limites ?"):
                    pass
            payload = json.loads(requests[0].content)
            system = payload["messages"][0]["content"]
            self.assertIn("fournisseur mistral", system)
            self.assertIn("modèle ministral-test", system)
            self.assertIn("aucun accès aux quotas", system)
            self.assertIn("Par défaut, réponds en texte Markdown", system)

    async def test_recent_interface_source_is_not_cut_inside_its_script(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = StorageManager(Path(directory) / "chat.db")
            conversation = storage.create_conversation("Jeu")
            interface = '```html\n<div id="game"></div><script>' + ('// état du jeu\n' * 200) + 'const endOfGame = true;</script>\n```'
            storage.add_message(conversation, "user", "Crée un jeu")
            storage.add_message(conversation, "assistant", interface)
            handler = ChatStreamHandler(storage=storage)
            with mock_sse() as requests:
                async for _ in handler.stream_chat(conversation, "Ajoute un bouton Recommencer"):
                    pass
                payload = json.loads(requests[0].content)
                previous = next(message for message in payload["messages"] if message["role"] == "assistant")
                self.assertEqual(previous["content"], interface)

    async def test_tokens_arrive_separately_without_blocking_event_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = StorageManager(Path(directory) / "chat.db")
            conversation = storage.create_conversation("Test flux")
            handler = ChatStreamHandler(storage=storage)
            with mock_sse(DELAYED_LINES, delay=0.06):
                stream = handler.stream_chat(conversation, "Salut")
                first = json.loads((await anext(stream)).split("data: ", 1)[1])
                self.assertEqual(first["event"], "status")

                waiting_for_token = asyncio.create_task(anext(stream))
                await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.04)
                self.assertFalse(waiting_for_token.done())
                token = json.loads((await waiting_for_token).split("data: ", 1)[1])
                self.assertEqual(token["html"], "Bon")
                rest = [json.loads(event.split("data: ", 1)[1]) async for event in stream]
                self.assertEqual([event["event"] for event in rest], ["token", "done"])
                self.assertEqual(storage.get_messages(conversation)[-1]["content"], "Bonjour")
