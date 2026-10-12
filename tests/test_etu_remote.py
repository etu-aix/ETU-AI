"""Remote identity, transport constraints and transient request isolation."""
import asyncio
import base64
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from integration.bridge import Bridge, digest
from integration.remote import RemoteClient, PairBody, SCOPES, canonical, validate_url
from integration import api as routes


class MemoryBridge(Bridge):
    def _read(self): return None
    def _save(self): pass


class Wire:
    def __init__(self): self.messages = []; self.closed = False
    async def send(self, wire): self.messages.append(json.loads(wire))
    async def close(self): self.closed = True


class RemoteSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.identity = MemoryBridge('unused')
        self.identity.identity()
        self.client = RemoteClient(self.identity)
        self.client.app = SimpleNamespace(state=SimpleNamespace(etu_storage=None))
        self.wire = self.client.ws = Wire()
        self.pair = str(uuid4())
        self.client.save({'pair_id':self.pair,'scopes':['ai.chat','workspace.read']})

    async def asyncTearDown(self):
        await self.client.stop()

    async def test_remote_startup_survives_unreadable_local_identity(self):
        identity = Bridge('unused')
        with patch.object(identity, 'identity', side_effect=OSError('unreadable DPAPI identity')):
            client = RemoteClient(identity)
            await client.start(SimpleNamespace())
            self.assertEqual(client.status()['status'], 'error')
            self.assertEqual(client.status()['error_code'], 'identity_unavailable')
            await client.stop()

    def test_internet_requires_wss_and_credentials_never_enter_url(self):
        for url in ['ws://evil.example/wss','wss://user:secret@example.com/wss','wss://example.com/wss?ticket=secret','wss://example.com/wss#secret','https://example.com/wss']:
            with self.assertRaises(ValueError): validate_url(url)
        with self.assertRaises(ValueError): validate_url('ws://127.0.0.1:8889')
        self.assertEqual(validate_url('ws://127.0.0.1:8889',True),'ws://127.0.0.1:8889')

    async def test_code_pairing_defaults_to_the_complete_integration(self):
        body = PairBody.model_validate({})
        self.assertEqual(set(body.scopes), set(SCOPES))
        self.client.save(None)
        with patch.object(self.client, 'run', new=self.idle):
            await self.client.create_pairing(body.scopes, body.device_name)
        self.assertEqual(set(self.client.scopes), set(SCOPES))
        self.assertTrue(self.client.device_name)

    async def idle(self):
        await asyncio.sleep(100)

    async def test_native_disconnect_waits_for_the_server_to_revoke_both_sides(self):
        self.client.state = 'connected'
        original_send = self.wire.send
        async def server(wire):
            await original_send(wire)
            if json.loads(wire)['type'] == 'device.disconnect':
                await self.client.receive({'type':'device.revoked'})
        self.wire.send = server
        await self.client.disconnect()
        self.assertIsNone(self.client.stored())
        self.assertTrue(self.wire.closed)
        self.assertEqual(self.wire.messages[-1]['type'], 'device.disconnect')

    async def test_offline_disconnect_keeps_the_association_until_revocation_is_possible(self):
        with self.assertRaises(HTTPException) as error:
            await self.client.disconnect()
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(self.client.stored()['pair_id'], self.pair)

    async def test_signature_is_real_ed25519_and_public_status_excludes_private_identity(self):
        nonce = base64.b64encode(b'n'*32).decode(); challenge=str(uuid4())
        await self.client.receive({'type':'device.challenge','challenge_id':challenge,'nonce':nonce})
        frame=self.wire.messages[-1]
        Ed25519PublicKey.from_public_bytes(base64.b64decode(self.identity.identity()['public_key'])).verify(base64.b64decode(frame['signature']),canonical(challenge,nonce,self.identity.identity()['instance_id'],self.pair))
        public=json.dumps(self.client.status())
        self.assertNotIn('private_key',public)
        self.assertNotIn(self.identity.record['private_key'],public)
        self.assertNotIn('Bearer',json.dumps(self.client.stored()))

    async def test_routes_and_scope_escalation_are_denied_without_creating_session(self):
        for payload in [{'path':'/api/settings'},{'path':'/v1/models'},{'path':'/v1/status','user_id':999}]:
            await self.client.execute(str(uuid4()),payload)
            self.assertIn(self.wire.messages[-1]['error']['code'],{'PERMISSION_DENIED','INVALID_MESSAGE'})
            self.assertFalse(self.identity.sessions)

    async def test_each_generation_has_separate_context_and_cancellation_cleans_its_session(self):
        entered=asyncio.Event(); release=asyncio.Event(); snapshots=[]
        async def generate(body,request):
            snapshots.append(routes.auth(request)['context'].copy())
            entered.set(); await release.wait()
            return {'model':'fixture','choices':[{'message':{'content':'done'},'finish_reason':'stop'}]}
        with patch.object(routes,'bridge',self.identity),patch.object(routes,'completions',generate):
            payload={'path':'/v1/chat/completions','method':'POST','body':{'messages':[{'role':'user','content':'fixture'}]},'context':{'workspace':{'file':{'content':'isolated'}}}}
            task=asyncio.create_task(self.client.execute(str(uuid4()),payload))
            await asyncio.wait_for(entered.wait(),2)
            self.assertEqual(snapshots[0]['workspace']['file']['content'],'isolated')
            self.assertEqual(len(self.identity.sessions),1)
            task.cancel(); await asyncio.gather(task,return_exceptions=True)
            self.assertFalse(self.identity.sessions)

    async def test_null_content_metadata_chunk_keeps_stream_alive(self):
        async def generate(body, request):
            async def events():
                yield 'data: {"choices":[{"delta":{"role":"assistant","content":null}}]}\n\n'
                yield 'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            return StreamingResponse(events())
        with patch.object(routes, 'bridge', self.identity), patch.object(routes, 'completions', generate):
            await self.client.execute(str(uuid4()), {'path':'/v1/chat/completions','method':'POST','body':{'messages':[{'role':'user','content':'fixture'}],'stream':True}})
        self.assertEqual(self.wire.messages[-1]['type'], 'ai.response.done')
        self.assertEqual(self.wire.messages[-1]['data']['choices'][0]['message']['content'], 'OK')
        self.assertFalse(self.identity.sessions)

    async def test_streamed_error_does_not_produce_a_done_frame(self):
        async def generate(body,request):
            async def events():
                yield 'data: {"error":{"code":"workspace_vision_failed"}}\n\ndata: [DONE]\n\n'
            return StreamingResponse(events())
        with patch.object(routes,'bridge',self.identity),patch.object(routes,'completions',generate):
            await self.client.execute(str(uuid4()),{'path':'/v1/chat/completions','method':'POST','body':{'messages':[{'role':'user','content':'fixture'}],'stream':True}})
        self.assertEqual(self.wire.messages[-1]['type'],'ai.response.error')
        self.assertEqual(self.wire.messages[-1]['error']['code'],'workspace_vision_failed')
        self.assertFalse(self.identity.sessions)

    async def test_revoked_pair_is_removed_and_a_mismatched_authentication_is_rejected(self):
        with self.assertRaises(ValueError):
            await self.client.receive({'type':'device.authenticated','pair_id':str(uuid4()),'scopes':['ai.chat']})
        await self.client.receive({'type':'device.revoked'})
        self.assertIsNone(self.client.stored())
        self.assertTrue(self.wire.closed)
        self.assertEqual(self.client.state,'revoked')


class VaultTests(unittest.TestCase):
    def test_remote_pair_survives_secure_vault_reload_without_replacing_local_grants(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'identity.dpapi'
            identity=Bridge(path);public=identity.identity()
            identity.record['grants']['fixture']={'origin':'https://etu-aix.alwaysdata.net','permissions':['etu.profile.group'],'token_hash':'fixture-hash'}
            client=RemoteClient(identity);client.save({'pair_id':str(uuid4()),'scopes':['ai.chat']})
            restored=Bridge(path);self.assertEqual(restored.identity(),public)
            self.assertIn('fixture',restored.record['grants'])
            self.assertEqual(restored.record['remote_pair'],client.stored())
            if path.exists(): self.assertNotIn(identity.record['private_key'].encode(),path.read_bytes())


if __name__=='__main__': unittest.main()
