import asyncio, json, os, sys, unittest
from pathlib import Path
from integration.tool_relay import ToolRelay
from integration.bridge import Bridge

class RelayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bridge = Bridge()
        self.bridge.sessions['session-a'] = {'pair_id':'pair-a', 'origin':'http://localhost:8888', 'permissions':['etu.profile.group','etu.schedule.read'], 'expires_at':self.bridge.clock()+1800}
        self.relay = ToolRelay()
        self.relay.register(self.bridge,'session-a',self.bridge.sessions['session-a'],[{'name':'profile.group'}, {'name':'schedule.read'}, {'name':'outlets.read'}])
    async def test_real_round_trip_does_not_contact_server_or_use_tokens(self):
        key=self.relay.binding()
        tools = self.relay.tools(key)
        self.assertEqual([v['function']['name'] for v in tools],['etu_profile_group','etu_schedule_read','etu_outlets_map_read'])
        outlet_schema = tools[2]['function']['parameters']
        self.assertEqual(outlet_schema['required'], ['query', 'group', 'room', 'id_text', 'has_outlets'])
        self.assertIn('query', outlet_schema['properties'])
        self.assertEqual(outlet_schema['properties']['query']['type'], ['string', 'null'])
        self.assertEqual(outlet_schema['properties']['has_outlets']['type'], ['boolean', 'null'])
        task=asyncio.create_task(self.relay.call(key,'etu_schedule_read',{'from':'2026-10-04','to':'2026-10-05'}))
        await asyncio.sleep(0)
        pending=self.relay.poll(key)[0]
        self.assertEqual(set(pending), {'request_id','tool','arguments'})
        self.relay.result(key,pending['request_id'],{'success':True,'data':{'events':[{'title':'Real course'}]}})
        self.assertEqual((await task)['data']['events'][0]['title'],'Real course')
        self.assertEqual(self.relay.poll(key),[])
    async def test_outlet_map_filters_are_relayed_as_optional_arguments(self):
        key = self.relay.binding()
        task = asyncio.create_task(self.relay.call(key, 'etu_outlets_map_read', {'query': 'A-204', 'has_outlets': True}))
        await asyncio.sleep(0)
        pending = self.relay.poll(key)[0]
        self.assertEqual(pending['tool'], 'outlets.read')
        self.assertEqual(pending['arguments'], {'query': 'A-204', 'has_outlets': True})
        self.relay.result(key, pending['request_id'], {'success': True, 'data': {'rooms': {}}})
        self.assertEqual((await task)['data']['rooms'], {})
    async def test_write_unknown_permission_and_extra_actor_are_denied(self):
        for tool,args in [('friends.request',{}),('etu_schedule_read',{'from':'2026-10-04','to':'2026-10-05','user_id':2})]:
            self.assertFalse((await self.relay.call('session-a',tool,args))['success'])
        from fastapi import HTTPException
        with self.assertRaises(HTTPException): self.relay.register(self.bridge,'session-a',self.bridge.sessions['session-a'],[{'name':'admin.delete'}])
    async def test_other_session_cannot_answer_a_request(self):
        task=asyncio.create_task(self.relay.call('session-a','etu_profile_group',{}))
        await asyncio.sleep(0)
        request=self.relay.poll('session-a')[0]
        from fastapi import HTTPException
        with self.assertRaises(HTTPException): self.relay.result('session-b',request['request_id'],{'success':True,'data':{}})
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
    async def test_revocation_expiry_and_ambiguous_accounts_hide_tools(self):
        self.bridge.sessions['session-b']={**self.bridge.sessions['session-a'],'pair_id':'pair-b'}
        self.relay.register(self.bridge,'session-b',self.bridge.sessions['session-b'],[{'name':'profile.group'}])
        self.assertIsNone(self.relay.binding())
        del self.bridge.sessions['session-b']
        self.assertEqual(self.relay.binding(),'session-a')
        self.bridge.sessions['session-a']['expires_at']=0
        self.assertEqual(self.relay.tools('session-a'),[])
        self.assertFalse((await self.relay.call('session-a','etu_profile_group',{}))['success'])

if __name__=='__main__': unittest.main()
