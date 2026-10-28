"""Exercise the registered backend functions using fake existing clients."""
from datetime import datetime, timezone
import sys
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import contextlib
import json
from pathlib import Path
from telethon.errors import FloodWaitError, RPCError
from mcp.types import CallToolResult, TextContent

from telethon.tl.types import Channel, ChatPhotoEmpty, User

from telegram_setup.search_pages import register_search_pages
from telegram_setup.gateway import UpstreamBridge, create_gateway
from telegram_setup.reader_gateway import ReaderGateway
from telegram_setup.agents import AccessDenied
from test_read_extensions import runtime
from test_agent_gateway import RegistryFixture


class SearchPageTests(unittest.IsolatedAsyncioTestCase):
    def register(self, client):
        rt = runtime(client)
        rt.resolve_entity.return_value = NS(forum=False)
        rt.get_marked_id = lambda e: e.id if hasattr(e, 'id') else 11
        rt.resolve_entity.return_value = NS(id=11, forum=False)
        rt.sanitize_name = lambda s: s
        converter = NS(message_to_dict=lambda m, ident, *_: {'id': m.id, 'chat_id': ident, 'text': 'fixture'})
        with patch.dict(sys.modules, {'telegram_mcp.tools.messages': converter}):
            register_search_pages(rt)
        return rt

    async def test_snapshot_cursor_descending_and_no_prefix_or_transcription(self):
        client = NS(get_messages=AsyncMock(side_effect=[[NS(id=10)], [NS(id=i) for i in (9, 7, 5)],
                                                       [NS(id=5)]]))
        rt = self.register(client); tool = rt.mcp.tools['read_search_page']
        first = await tool(11, 'fixture', limit=2)
        self.assertEqual(first['until_id'], 10); self.assertEqual(first['next_before_id'], 7)
        second = await tool(11, 'fixture', before_id=7, until_id=10, limit=2)
        self.assertFalse(second['has_more']); self.assertEqual(second['messages'][0]['id'], 5)
        self.assertEqual(client.get_messages.await_args.kwargs,
                         {'search': 'fixture', 'limit': 3, 'max_id': 7})

    async def test_disallowed_or_wrong_identity_never_reads_search(self):
        client = NS(get_messages=AsyncMock())
        rt = self.register(client); rt.is_chat_allowlist_enabled = lambda: True
        rt.is_chat_allowed = lambda *_: False
        with self.assertRaises(ValueError): await rt.mcp.tools['read_search_page'](11, 'fixture')
        rt.resolve_entity.return_value = NS(id=22)
        with self.assertRaises(ValueError): await rt.mcp.tools['read_search_page'](11, 'fixture')
        client.get_messages.assert_not_awaited()

    async def test_public_search_discards_users_bots_private_and_forbidden(self):
        channel = Channel(id=11, title='Public synthetic', photo=ChatPhotoEmpty(),
                          date=datetime.now(timezone.utc), username='fixture_channel', broadcast=True)
        private = Channel(id=22, title='Private synthetic', photo=ChatPhotoEmpty(),
                          date=datetime.now(timezone.utc), megagroup=True)
        class Client:
            def __init__(self): self.calls = []
            async def __call__(self, request, **kwargs):
                assert kwargs == {'flood_sleep_threshold': 0}
                self.calls.append(request)
                return NS(chats=[channel, private], users=[User(id=42, first_name='Never exposed', bot=True)])
        client = Client(); rt = self.register(client)
        rt.get_marked_id = lambda entity: -1000000 - entity.id
        result = await rt.mcp.tools['search_community_metadata']('fixture')
        self.assertEqual(len(result['communities']), 1)
        self.assertEqual(result['communities'][0]['username'], 'fixture_channel')
        self.assertFalse(result['history_read']); self.assertFalse(result['joined'])
        self.assertEqual(type(client.calls[0]).__name__, 'SearchRequest')
        self.assertEqual(len(client.calls), 1)

    async def test_empty_chat_stops_and_query_boundaries_fail_before_network(self):
        client = NS(get_messages=AsyncMock(return_value=[])); rt = self.register(client)
        result = await rt.mcp.tools['read_search_page'](11, 'fixture')
        self.assertEqual(result['messages'], []); self.assertFalse(result['has_more'])
        before = client.get_messages.await_count
        for query in ('', ' ' * 3, 'x' * 501):
            with self.assertRaises(ValueError): await rt.mcp.tools['read_search_page'](11, query)
        self.assertEqual(client.get_messages.await_count, before)

    async def test_public_flood_wait_returns_seconds_and_never_echoes_rpc_message(self):
        class Client:
            calls=0
            async def __call__(self,request,**kwargs):
                self.calls+=1
                assert kwargs=={'flood_sleep_threshold':0}
                raise FloodWaitError(request=None,capture=19)
        client=Client();rt=self.register(client)
        result=await rt.mcp.tools['search_community_metadata']('fixture',limit=3)
        self.assertEqual(result,{'error':'telegram_cooldown','leaf_error_category':'flood_wait',
                                'leaf_error_type':'FloodWaitError','retry_after_seconds':19})
        self.assertEqual(client.calls,1)

    async def test_public_error_categories_only_no_exception_text_or_entities(self):
        cases=[(TimeoutError('sensitive-test-value'),'transport','TimeoutError'),
               (ConnectionError('sensitive-test-value'),'transport','ConnectionError'),
               (RPCError(None,'sensitive-test-value'),'telegram_rpc','RPCError'),
               (AttributeError('sensitive-test-value'),'backend_exception','AttributeError')]
        for error,category,kind in cases:
            class Client:
                async def __call__(self,*args,**kwargs):raise error
            rt=self.register(Client())
            result=await rt.mcp.tools['search_community_metadata']('fixture',limit=3)
            self.assertEqual(result,{'error':'public_search_failed','leaf_error_category':category,'leaf_error_type':kind})
            self.assertNotIn('sensitive-test-value',json.dumps(result))

    async def test_rpc_420_wait_is_preserved_without_assuming_only_one_error_subclass(self):
        class OtherWait(RPCError):
            def __init__(self):super().__init__(None,'not-logged',code=420);self.seconds=23
        class Client:
            async def __call__(self,*args,**kwargs):raise OtherWait()
        rt=self.register(Client())
        result=await rt.mcp.tools['search_community_metadata']('fixture',limit=3)
        self.assertEqual(result['error'],'telegram_cooldown');self.assertEqual(result['retry_after_seconds'],23)
        self.assertEqual(result['leaf_error_type'],'OtherWait')


class PublicSearchPipelineTests(RegistryFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):self.setup_registry()
    def tearDown(self):self.teardown_registry()

    def grant(self):
        self.data['agents']['a']['actions'].append('public-search');self.registry.save(self.data)

    def api(self,result,available=None):
        session=NS(call_tool=AsyncMock(return_value=CallToolResult(content=[TextContent(type='text',text=json.dumps(result))])))
        bridge=UpstreamBridge(session,available_tools=available)
        return ReaderGateway(self.registry,bridge),session

    async def test_no_grant_blocks_before_backend_even_with_capability(self):
        api,session=self.api({}, {'search_community_metadata'})
        with self.assertRaises(AccessDenied):await api.public_communities('a','fixture',3)
        session.call_tool.assert_not_awaited()

    async def test_missing_backend_capability_is_explicit_and_no_rpc_is_dispatched(self):
        self.grant();api,session=self.api({},set())
        result=await api.public_communities('a','fixture',3)
        self.assertEqual(result,{'error':'public_search_backend_unavailable','leaf_error_category':'capability_missing',
                                'leaf_error_type':'ToolNotExposed'})
        session.call_tool.assert_not_awaited()

    async def test_flood_wait_survives_bridge_facade_and_blocks_immediate_repeat(self):
        self.grant()
        api,session=self.api({'error':'telegram_cooldown','retry_after_seconds':19,
            'leaf_error_category':'flood_wait','leaf_error_type':'FloodWaitError'},{'search_community_metadata'})
        first=await api.public_communities('a','fixture',3)
        self.assertEqual(first['leaf_error_type'],'FloodWaitError')
        self.assertGreaterEqual(first['retry_after_seconds'],19)
        again=await api.public_communities('a','fixture',3)
        self.assertEqual(again['error'],'telegram_cooldown');self.assertEqual(session.call_tool.await_count,1)

    async def test_only_safe_public_diagnostic_fields_survive(self):
        self.grant();api,session=self.api({'error':'public_search_failed','leaf_error_category':'telegram_rpc',
            'leaf_error_type':'RPCError','raw_error':'sensitive-test-value','users':[{'phone':'sensitive-test-value'}]},
            {'search_community_metadata'})
        result=await api.public_communities('a','fixture',3)
        self.assertEqual(result,{'error':'public_search_failed','leaf_error_category':'telegram_rpc','leaf_error_type':'RPCError'})
        self.assertNotIn('sensitive-test-value',json.dumps(result))

    async def test_public_tool_metadata_stays_readonly_and_ten_tool_contract(self):
        @contextlib.asynccontextmanager
        async def upstream():yield self.upstream
        server=create_gateway(self.registry,'a',upstream,enable_saved_text=True,enable_search=True,enable_file_probe=True)
        tools={t.name:t for t in await server.list_tools()}
        self.assertEqual(len(tools),10)
        self.assertTrue(tools['search_public_communities'].annotations.readOnlyHint)
        self.assertEqual(set(tools['search_public_communities'].inputSchema['properties']),{'query','limit'})
