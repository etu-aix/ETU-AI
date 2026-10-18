"""The local chat must discover real ETU tools and use browser results."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent))
from chat.sse_handler import ChatStreamHandler
from storage.db import StorageManager
from integration.tool_relay import ToolRelay
from integration.bridge import Bridge
from mock_sse import mock_sse

class ChatToolTest(unittest.IsolatedAsyncioTestCase):
    async def test_chat_discovers_tools_and_uses_browser_result(self):
        bridge = Bridge()
        bridge.sessions['session'] = {'pair_id':'pair', 'origin':'http://localhost:8888', 'permissions':['etu.profile.group'], 'expires_at':bridge.clock()+1800}
        relay = ToolRelay()
        relay.register(bridge, 'session', bridge.sessions['session'], [{'name':'profile.group'}])
        async def browser():
            for _ in range(200):
                pending = relay.poll('session')
                if pending:
                    relay.result('session', pending[0]['request_id'], {'success':True, 'data':{'group':'Group1'}})
                    return
                await asyncio.sleep(.01)
            raise AssertionError('No tool request arrived')
        tool_lines = [b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"tool-1","function":{"name":"etu_profile_group","arguments":"{}"}}]}}]}\n\n', b'data: [DONE]\n\n']
        answer_lines = [b'data: {"choices":[{"delta":{"content":"Votre groupe est Group1."}}]}\n\n', b'data: [DONE]\n\n']
        with tempfile.TemporaryDirectory() as directory:
            storage = StorageManager(Path(directory)/'chat.db')
            conversation = storage.create_conversation('Validation outils')
            with patch('chat.sse_handler.tool_relay', relay), mock_sse([tool_lines, answer_lines]) as requests:
                browser_task = asyncio.create_task(browser())
                events = [json.loads(event.split('data: ',1)[1]) async for event in ChatStreamHandler(storage=storage).stream_chat(conversation, 'Quel est mon groupe ?')]
                await browser_task
            payload = json.loads(requests[0].content)
            self.assertEqual(payload['tools'][0]['function']['name'], 'etu_profile_group')
            self.assertIn('ETU 2 est connecté', payload['messages'][0]['content'])
            result = next(event['result'] for event in events if event['event']=='tool_result')
            self.assertEqual(result['data']['group'], 'Group1')
            synthesis = json.loads(requests[1].content)
            self.assertEqual(json.loads(synthesis['messages'][-1]['content'])['data']['group'], 'Group1')

    async def test_etu_tools_are_not_offered_without_remote_sharing_consent(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = StorageManager(Path(directory)/'chat.db')
            storage.set_setting('api_providers', {'active_provider':'test', 'configs':{'test':{'base_url':'https://example.test/v1', 'model':'test'}}})
            storage.set_setting('etu_remote_tools_enabled', False)
            conversation = storage.create_conversation('Test distant')
            with patch('chat.sse_handler.get_api_key', return_value='dummy-key'), patch('chat.sse_handler.tool_relay.binding', side_effect=AssertionError('ETU data must stay local')), mock_sse() as requests:
                async for _ in ChatStreamHandler(storage=storage).stream_chat(conversation, 'Bonjour'): pass
            payload = json.loads(requests[0].content)
            self.assertIsNone(payload['tools'])
            self.assertIn('Le partage des informations ETU avec les fournisseurs tiers est désactivé', payload['messages'][0]['content'])
            self.assertIn('section Fournisseurs IA et clés API', payload['messages'][0]['content'])

    async def test_remote_tools_require_consent_and_are_offered_when_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = StorageManager(Path(directory)/'chat.db')
            storage.set_setting('api_providers', {'active_provider':'mistral', 'configs':{'mistral':{'base_url':'https://api.mistral.ai/v1', 'model':'mistral-small-latest', 'type':'openai'}}})
            storage.set_setting('etu_remote_tools_enabled', True)
            conversation = storage.create_conversation('Test consentement distant')
            catalogue = [{'type':'function','function':{'name':'etu_schedule_read','description':'Lire le calendrier','parameters':{'type':'object','properties':{},'required':[]}}}]
            with patch('chat.sse_handler.get_api_key', return_value='dummy-key'), patch('chat.sse_handler.tool_relay.binding', return_value='binding'), patch('chat.sse_handler.tool_relay.tools', return_value=catalogue), mock_sse() as requests:
                async for _ in ChatStreamHandler(storage=storage).stream_chat(conversation, 'Quel est mon prochain cours ?'): pass
            payload = json.loads(requests[0].content)
            self.assertEqual(payload['tools'], catalogue)
            self.assertIn('Bearer dummy-key', requests[0].headers['Authorization'])
