"""Stopping a generation closes the upstream stream and retains partial answers."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

import api_server
from chat.sse_handler import ChatStreamHandler
from storage.db import StorageManager


class PendingSSE(httpx.AsyncByteStream):
    def __init__(self, first_token=None):
        self.first_token = first_token
        self.waiting = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        if self.first_token:
            yield ('data: ' + json.dumps({"choices": [{"delta": {"content": self.first_token}}]}) + '\n\n').encode()
        self.waiting.set()
        await asyncio.Event().wait()
        yield b""

    async def aclose(self):
        self.closed = True


class TestGenerationStop(unittest.IsolatedAsyncioTestCase):
    async def check_stop(self, partial):
        with tempfile.TemporaryDirectory() as directory:
            storage = StorageManager(Path(directory) / "chat.db")
            conversation = storage.create_conversation()
            handler = ChatStreamHandler(storage=storage)
            upstream = PendingSSE(partial)
            request_id = str(uuid4())
            client_type = httpx.AsyncClient
            transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=upstream))

            async def consume():
                async for _ in handler.stream_chat(conversation, "Explique", request_id=request_id):
                    pass

            with patch("chat.sse_handler.httpx.AsyncClient", side_effect=lambda **kwargs: client_type(transport=transport, **kwargs)):
                task = asyncio.create_task(consume())
                await asyncio.wait_for(upstream.waiting.wait(), timeout=1)
                with patch.object(api_server, "stream_handler", handler):
                    self.assertEqual(await api_server.stop_chat(uuid4()), {"cancelled": False})
                    self.assertEqual(await api_server.stop_chat(api_server.UUID(request_id)), {"cancelled": True})
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=0.2)
            self.assertTrue(upstream.closed)
            self.assertEqual(handler.active_requests, {})
            messages = storage.get_messages(conversation)
            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[-1]["content"], partial or "Génération interrompue.")
            self.assertFalse(handler.cancel(request_id))

    async def test_stop_while_thinking_without_tokens(self):
        await self.check_stop(None)

    async def test_stop_retains_partial_answer(self):
        await self.check_stop("Début de la réponse")

    async def test_stop_while_connecting(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = StorageManager(Path(directory) / "chat.db")
            handler = ChatStreamHandler(storage=storage)
            conversation = storage.create_conversation()
            connecting = asyncio.Event()

            async def connect(request):
                connecting.set()
                await asyncio.Event().wait()

            client_type = httpx.AsyncClient
            transport = httpx.MockTransport(connect)
            request_id = str(uuid4())

            async def consume():
                async for _ in handler.stream_chat(conversation, "Explique", request_id=request_id):
                    pass

            with patch("chat.sse_handler.httpx.AsyncClient", side_effect=lambda **kwargs: client_type(transport=transport, **kwargs)):
                task = asyncio.create_task(consume())
                await asyncio.wait_for(connecting.wait(), timeout=1)
                self.assertTrue(handler.cancel(request_id))
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=0.2)
            self.assertEqual(handler.active_requests, {})

    async def test_disconnect_releases_conversation_and_saves_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = StorageManager(Path(directory) / "chat.db")
            conversation = storage.create_conversation()
            handler = ChatStreamHandler(storage=storage)
            upstream = PendingSSE("Bonjour")
            client_type = httpx.AsyncClient
            transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=upstream))
            with patch.object(api_server, "storage", storage), patch.object(api_server, "stream_handler", handler), patch("chat.sse_handler.httpx.AsyncClient", side_effect=lambda **kwargs: client_type(transport=transport, **kwargs)):
                response = await api_server.chat_stream_endpoint(api_server.ChatRequest(conversation_id=conversation, message="Bonjour"))
                await anext(response.body_iterator)
                await anext(response.body_iterator)
                await response.body_iterator.aclose()
                self.assertNotIn(conversation, api_server._active_chat_conversations)
            self.assertTrue(upstream.closed)
            self.assertEqual(handler.active_requests, {})
            self.assertEqual(storage.get_messages(conversation)[-1]["content"], "Bonjour")
