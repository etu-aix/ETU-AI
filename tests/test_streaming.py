"""Le flux local ne doit pas bloquer les autres requêtes FastAPI."""

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chat.sse_handler import ChatStreamHandler
from storage.db import StorageManager


class DelayedResponse:
    def __init__(self):
        self.lines = iter([
            b'data: {"choices":[{"delta":{"content":"Bon"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"jour"}}]}\n',
            b'data: [DONE]\n',
        ])

    def readline(self):
        time.sleep(0.06)
        return next(self.lines, b"")

    def close(self):
        pass


class TestStreaming(unittest.IsolatedAsyncioTestCase):
    async def test_recent_interface_source_is_not_cut_inside_its_script(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = StorageManager(Path(directory) / "chat.db")
            conversation = storage.create_conversation("Jeu")
            interface = '```html\n<div id="game"></div><script>' + ('// état du jeu\n' * 200) + 'const endOfGame = true;</script>\n```'
            storage.add_message(conversation, "user", "Crée un jeu")
            storage.add_message(conversation, "assistant", interface)
            handler = ChatStreamHandler(storage=storage)
            with patch("urllib.request.urlopen", return_value=DelayedResponse()) as request:
                async for _ in handler.stream_chat(conversation, "Ajoute un bouton Recommencer"):
                    pass
                payload = json.loads(request.call_args.args[0].data)
                previous = next(message for message in payload["messages"] if message["role"] == "assistant")
                self.assertEqual(previous["content"], interface)

    async def test_tokens_arrive_separately_without_blocking_event_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = StorageManager(Path(directory) / "chat.db")
            conversation = storage.create_conversation("Test flux")
            handler = ChatStreamHandler(storage=storage)
            with patch("urllib.request.urlopen", return_value=DelayedResponse()):
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
