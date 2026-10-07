"""Une discussion conserve une seule génération, même avec plusieurs onglets."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
import api_server
from storage.db import StorageManager


class TestChatConcurrency(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_request_is_rejected_and_lock_is_released(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = StorageManager(Path(directory) / "chat.db")
            conversation = storage.create_conversation("Test")
            closed = []

            async def stream(*args, **kwargs):
                try:
                    yield 'data: {"event":"token","html":"Bonjour"}\n\n'
                    yield 'data: {"event":"done"}\n\n'
                finally:
                    closed.append(True)

            request = api_server.ChatRequest(conversation_id=conversation, message="Bonjour")
            with patch.object(api_server, "storage", storage), patch.object(api_server.stream_handler, "stream_chat", stream):
                first = await api_server.chat_stream_endpoint(request)
                await anext(first.body_iterator)
                with self.assertRaises(HTTPException) as conflict:
                    await api_server.chat_stream_endpoint(request)
                self.assertEqual(conflict.exception.status_code, 409)
                await first.body_iterator.aclose()
                self.assertEqual(closed, [True])
                self.assertNotIn(conversation, api_server._active_chat_conversations)
                second = await api_server.chat_stream_endpoint(request)
                self.assertEqual(len([event async for event in second.body_iterator]), 2)
                self.assertEqual(closed, [True, True])
                self.assertNotIn(conversation, api_server._active_chat_conversations)
