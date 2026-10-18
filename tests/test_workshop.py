"""Workshop transport, portable profiles and downloads without network or user data."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from integration import workshop
from integration.remote import RemoteClient, PairBody, SCOPES
from storage.db import StorageManager


class WorkshopTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.storage = StorageManager(Path(self.directory.name) / 'profiles.db')
        self.downloader = Mock()
        self.downloader.list_installed_models.return_value = []
        self.downloader.start_download.return_value = 'download-task'
        self.server = SimpleNamespace(storage=self.storage, downloader=self.downloader,
            ProfileRequest=lambda **kw: SimpleNamespace(**kw),
            validate_profile=lambda profile: (profile.name, profile.avatar, profile.instructions))
        self.patches = [patch.dict('sys.modules', {'api_server': self.server}),
            patch.object(workshop, 'auth', return_value={'pair_id': 'paired-account', 'permissions': ['etu.workshop.profiles']}),
            patch.object(workshop, 'admin')]
        for item in self.patches: item.start()
        app = FastAPI()
        app.include_router(workshop.router)
        self.client = TestClient(app)
        self.body = {'format': 'etu-ai-profile', 'version': 1, 'workshop_id': 42,
            'name': 'Tuteur', 'avatar': '✦', 'description': 'Un tuteur de code.', 'instructions': 'Explique les étapes.',
            'model': {'repo': 'owner/model', 'filename': 'model.Q4_K_M.gguf'}}

    def tearDown(self):
        self.client.close()
        for item in reversed(self.patches): item.stop()
        self.directory.cleanup()

    def test_import_saves_profile_and_uses_etu_ai_downloader(self):
        response = self.client.post('/v1/workshop/import', json=self.body)
        self.assertEqual(response.status_code, 200)
        profile = response.json()['profile']
        self.assertEqual(profile['instructions'], self.body['instructions'])
        self.assertEqual(profile['model'], self.body['model']['filename'])
        self.downloader.start_download.assert_called_once_with('owner/model', 'model.Q4_K_M.gguf')
        repeated = self.client.post('/v1/workshop/import', json=self.body)
        self.assertEqual(profile['id'], repeated.json()['profile']['id'])

    def test_import_does_not_download_installed_models_or_profiles_without_model(self):
        self.downloader.list_installed_models.return_value = ['model.Q4_K_M.gguf']
        self.assertEqual(self.client.post('/v1/workshop/import', json=self.body).status_code, 200)
        self.assertEqual(self.client.post('/v1/workshop/import', json={**self.body, 'model': None}).status_code, 200)
        self.downloader.start_download.assert_not_called()

    def test_permission_format_and_paths_rejected_before_downloading(self):
        with patch.object(workshop, 'auth', return_value={'pair_id': 'paired-account', 'permissions': ['etu.workspace.read']}):
            self.assertEqual(self.client.post('/v1/workshop/import', json=self.body).status_code, 403)
        for change in [{'version': 2}, {'model': {'repo': 'owner/model', 'filename': '../model.gguf'}}, {'format': 'unknown'}]:
            self.assertEqual(self.client.post('/v1/workshop/import', json={**self.body, **change}).status_code, 422)
        self.downloader.start_download.assert_not_called()

    def test_publication_uses_saved_profile_and_only_portable_fields(self):
        profile = self.storage.save_profile('Tuteur', '✦', 'Explique.', 'api:private-provider/model')
        with patch.object(workshop.remote, 'publish_profile', new_callable=AsyncMock, return_value={'profile_id': 9}) as send:
            response = self.client.post('/api/profiles/' + profile['id'] + '/workshop', json={
                'description': 'Tuteur public', 'model': None, 'accept_workshop_terms': True})
            self.assertEqual(response.status_code, 200)
            payload = send.call_args.args[0]
            self.assertEqual(set(payload), {'name', 'avatar', 'instructions', 'source_id', 'description', 'model', 'accept_workshop_terms'})
            self.assertEqual(payload['source_id'], profile['id'])
            self.assertNotIn('private-provider', json.dumps(payload))
            rejected = self.client.post('/api/profiles/' + profile['id'] + '/workshop', json={'description': 'Tuteur'})
            self.assertEqual(rejected.status_code, 422)


class WorkshopTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_publication_requires_connection_and_scope_then_waits_for_ack(self):
        client = RemoteClient()
        with patch.object(client, 'stored', return_value={'pair_id': 'pair', 'scopes': []}):
            with self.assertRaises(HTTPException): await client.publish_profile({})
            client.state = 'connected'
            client.ws = SimpleNamespace(send=AsyncMock())
            with self.assertRaises(HTTPException): await client.publish_profile({})
        with patch.object(client, 'stored', return_value={'pair_id': 'pair', 'scopes': ['workshop.profiles']}):
            task = asyncio.create_task(client.publish_profile({'name': 'Tuteur'}))
            await asyncio.sleep(0)
            wire = json.loads(client.ws.send.call_args.args[0])
            self.assertEqual(wire['type'], 'workshop.publish')
            self.assertFalse(task.done())
            await client.receive({'type': 'workshop.published', 'request_id': wire['request_id'], 'data': {'profile_id': 42}})
            self.assertEqual(await task, {'profile_id': 42})
            self.assertEqual(client.publications, {})

    async def test_default_pairing_includes_workshop(self):
        self.assertIn('workshop.profiles', PairBody().scopes)
        self.assertEqual(set(PairBody().scopes), set(SCOPES))


if __name__ == '__main__':
    unittest.main()
