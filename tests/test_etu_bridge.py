"""Exercise consent, cryptographic identity, isolation and expiry without a LLM."""
import base64
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi.testclient import TestClient
import api_server
import integration.api as routes
import integration.bridge as bridge_module
from integration.bridge import Bridge, digest

ORIGIN = "https://etu-aix.alwaysdata.net"
LOCAL = "http://127.0.0.1:11346"


class TestBridge(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.now = 1000
        self.bridge = Bridge(Path(self.directory.name) / "identity.dpapi", clock=lambda: self.now)
        self.patches = [patch.object(routes, "bridge", self.bridge), patch.object(api_server, "bridge", self.bridge)]
        for item in self.patches:
            item.start()
        self.client = TestClient(api_server.app, base_url=LOCAL)
        self.client.get("/")

    def tearDown(self):
        self.client.close()
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def pair(self, permissions=None, approve=True):
        response = self.client.post('/v1/pair', headers={'Origin': ORIGIN}, json={'permissions': permissions or ['etu.profile.group', 'etu.schedule.read', 'etu.workspace.read']})
        self.assertEqual(response.status_code, 202, response.text)
        pair = response.json()
        if approve:
            result = self.client.post('/api/etu/pairings/' + pair['pair_id'], headers={'Origin': LOCAL}, json={'approve': True})
            self.assertEqual(result.status_code, 200, result.text)
        return pair

    def test_localhost_origin_requires_explicit_local_mode(self):
        with patch.object(bridge_module, 'ALLOW_LOCALHOST_ORIGINS', False):
            self.assertFalse(bridge_module.is_etu_origin('http://localhost:8888'))
        with patch.object(bridge_module, 'ALLOW_LOCALHOST_ORIGINS', True):
            self.assertTrue(bridge_module.is_etu_origin('http://localhost:8888'))
            self.assertFalse(bridge_module.is_etu_origin('http://attacker.localhost:8888'))

    def session(self, pair):
        headers = {'Origin': ORIGIN, 'Authorization': 'Bearer ' + pair['pairing_token']}
        challenge = self.client.post('/v1/challenge', headers=headers, json={'nonce': 'a' * 43})
        self.assertEqual(challenge.status_code, 200, challenge.text)
        signed = challenge.json()
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(pair['identity']['public_key']))
        payload = base64.b64decode(signed['signed_payload'])
        public_key.verify(base64.b64decode(signed['signature']), payload)
        self.assertEqual(json.loads(payload)['origin'], ORIGIN)
        result = self.client.post('/v1/session', headers=headers, json={'challenge_id': signed['challenge_id']})
        self.assertEqual(result.status_code, 200, result.text)
        replay = self.client.post('/v1/session', headers=headers, json={'challenge_id': signed['challenge_id']})
        self.assertEqual(replay.status_code, 401)
        return {'Origin': ORIGIN, 'Authorization': 'Bearer ' + result.json()['access_token']}

    def test_consent_is_required_and_cannot_be_remote(self):
        pair = self.pair(approve=False)
        headers = {'Origin': ORIGIN, 'Authorization': 'Bearer ' + pair['pairing_token']}
        self.assertEqual(self.client.post('/v1/challenge', headers=headers, json={'nonce': 'a' * 43}).status_code, 401)
        path = '/api/etu/pairings/' + pair['pair_id']
        self.assertEqual(self.client.post(path, headers={'Origin': ORIGIN}, json={'approve': True}).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.client.post(path, headers={'Origin': LOCAL}, json={'approve': True}).status_code, 403)
        self.client.get('/')
        self.assertEqual(self.client.post(path, json={'approve': True}).status_code, 403)
        self.assertEqual(self.client.post(path, headers={'Origin': LOCAL}, json={'approve': False}).status_code, 200)
        self.assertEqual(self.client.post('/v1/challenge', headers=headers, json={'nonce': 'a' * 43}).status_code, 401)

    def test_identity_persists_encrypted_and_sessions_do_not(self):
        pair = self.pair()
        self.session(pair)
        another = Bridge(self.bridge.path)
        self.assertEqual(another.identity(), self.bridge.identity())
        self.assertEqual(len(another.record['grants']), 1)
        self.assertFalse(another.sessions)
        if self.bridge.path.exists():
            self.assertNotIn(self.bridge.record['private_key'].encode(), self.bridge.path.read_bytes())
            self.assertNotIn(pair['pairing_token'].encode(), self.bridge.path.read_bytes())

    def test_permissions_origins_host_and_request_size(self):
        for permission in ['etu.workspace.write', 'etu.schedule.write', 'etu.navigation.execute', 'etu.profile.write']:
            self.assertEqual(self.client.post('/v1/pair', headers={'Origin': ORIGIN}, json={'permissions': [permission]}).status_code, 422)
        self.assertEqual(self.client.post('/v1/pair', json={'permissions': ['etu.profile.group']}).status_code, 403)
        self.assertEqual(self.client.get('/v1/identity', headers={'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(self.client.get('/v1/identity', headers={'Host': 'evil.example'}).status_code, 400)
        self.assertEqual(self.client.get('/api/conversations', headers={'Origin': ORIGIN}).status_code, 403)
        self.assertEqual(self.client.post('/v1/pair', headers={'Origin': ORIGIN, 'Content-Type': 'application/json'}, content=b'x' * 262145).status_code, 413)
        response = self.client.options('/v1/pair', headers={'Origin': ORIGIN, 'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'content-type'})
        self.assertEqual(response.headers['access-control-allow-origin'], ORIGIN)

    def test_context_permissions_isolation_clear_expire_revoke(self):
        pair = self.pair(['etu.profile.group'])
        first = self.session(pair)
        second = self.session(pair)
        self.assertEqual(self.client.put('/v1/context', headers=first, json={'workspace': {'file': 'private'}}).status_code, 403)
        self.assertEqual(self.client.put('/v1/context', headers=first, json={'group': '1G1A'}).status_code, 200)
        self.assertEqual(self.bridge.authenticate(ORIGIN, second['Authorization'][7:])['context'], {})
        self.assertEqual(self.client.put('/v1/context', headers={**first, 'Origin': 'http://localhost:5173'}, json={'group': '2'}).status_code, 403)
        self.assertEqual(self.client.delete('/v1/context', headers=first).status_code, 200)
        self.assertEqual(self.bridge.authenticate(ORIGIN, first['Authorization'][7:])['context'], {})
        self.client.delete('/v1/session', headers=second)
        self.assertNotIn(digest(second['Authorization'][7:]), self.bridge.sessions)
        self.client.delete('/api/etu/pairings/' + pair['pair_id'], headers={'Origin': LOCAL})
        self.assertFalse(self.bridge.sessions)
        self.assertEqual(self.client.put('/v1/context', headers=first, json={'group': '1'}).status_code, 401)
        pair = self.pair()
        headers = self.session(pair)
        self.now += 1801
        self.assertEqual(self.client.delete('/v1/context', headers=headers).status_code, 401)
        self.assertFalse(self.bridge.sessions)

    def test_pending_and_challenge_expire(self):
        pair = self.pair(approve=False)
        self.now += 301
        self.assertEqual(self.client.post('/api/etu/pairings/' + pair['pair_id'], headers={'Origin': LOCAL}, json={'approve': True}).status_code, 404)
        pair = self.pair()
        headers = {'Origin': ORIGIN, 'Authorization': 'Bearer ' + pair['pairing_token']}
        challenge = self.client.post('/v1/challenge', headers=headers, json={'nonce': 'a' * 43}).json()
        self.now += 61
        self.assertEqual(self.client.post('/v1/session', headers=headers, json={'challenge_id': challenge['challenge_id']}).status_code, 401)

    def test_chat_is_local_ephemeral_and_has_no_tools(self):
        headers = self.session(self.pair())
        self.client.put('/v1/context', headers=headers, json={'group': '1G1A', 'workspace': {'file': {'name': 'main.py', 'content': 'secret code'}}})
        response = routes.httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'bonjour'}}]}, request=routes.httpx.Request('POST', 'http://127.0.0.1:11343/v1/chat/completions'))
        with patch.object(routes.httpx.AsyncClient, 'post', new=AsyncMock(return_value=response)) as post:
            result = self.client.post('/v1/chat/completions', headers=headers, json={'messages': [{'role': 'user', 'content': 'Mon groupe ?'}]})
        self.assertEqual(result.status_code, 200, result.text)
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'http://127.0.0.1:11343/v1/chat/completions')
        self.assertNotIn('tools', kwargs['json'])
        self.assertIn('secret code', json.dumps(kwargs['json']))
        self.assertEqual(self.client.post('/v1/chat/completions', headers=headers, json={'messages': [{'role': 'user', 'content': 'test'}], 'tools': []}).status_code, 422)

    def test_stream_and_revocation_during_silent_generation(self):
        pair = self.pair()
        headers = self.session(pair)
        bridge = self.bridge
        cancelled = []
        class Response:
            def raise_for_status(self):
                pass
            async def aiter_lines(self):
                yield 'data: {"choices":[{"delta":{"content":"Bonjour"}}]}'
                bridge.revoke(pair['pair_id'])
                try:
                    await asyncio.sleep(10)
                finally:
                    cancelled.append(True)
                yield 'data: [DONE]'
        class Stream:
            async def __aenter__(self):
                return Response()
            async def __aexit__(self, *args):
                pass
        with patch.object(routes.httpx.AsyncClient, 'stream', return_value=Stream()):
            result = self.client.post('/v1/chat/completions', headers=headers, json={'messages': [{'role': 'user', 'content': 'bonjour'}], 'stream': True})
        self.assertEqual(result.status_code, 200)
        self.assertIn('Bonjour', result.text)
        self.assertIn('"code": 401', result.text)
        self.assertIn('data: [DONE]', result.text)
        self.assertTrue(cancelled)

    def test_chat_reports_unavailable_engine(self):
        headers = self.session(self.pair())
        with patch.object(routes.httpx.AsyncClient, 'post', new=AsyncMock(side_effect=routes.httpx.ConnectError('offline'))):
            result = self.client.post('/v1/chat/completions', headers=headers, json={'messages': [{'role': 'user', 'content': 'bonjour'}]})
        self.assertEqual(result.status_code, 503)


if __name__ == '__main__':
    unittest.main()
