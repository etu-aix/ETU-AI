"""Transport SSE asynchrone isolé du réseau pour les tests du chat."""
import asyncio
from contextlib import contextmanager
from unittest.mock import patch

import httpx


class SSEBytes(httpx.AsyncByteStream):
    def __init__(self, lines, delay=0):
        self.lines = lines
        self.delay = delay
        self.closed = False

    async def __aiter__(self):
        for line in self.lines:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield line

    async def aclose(self):
        self.closed = True


@contextmanager
def mock_sse(lines=None, delay=0):
    requests = []
    lines = lines or [b'data: {"choices":[{"delta":{"content":"Bonjour"}}]}\n\n', b'data: [DONE]\n\n']

    async def handler(request):
        requests.append(request)
        response_lines = lines
        if lines and isinstance(lines[0], (list, tuple)):
            response_lines = lines[min(len(requests) - 1, len(lines) - 1)]
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=SSEBytes(response_lines, delay))

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    with patch("chat.sse_handler.httpx.AsyncClient", side_effect=lambda **kwargs: client_type(transport=transport, **kwargs)):
        yield requests
