"""Real backend tool manager and gateway over a synthetic Telegram client."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from mcp.server.fastmcp import FastMCP
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ToolAnnotations

from telegram_setup.gateway import create_gateway
from telegram_setup.client import text_payload
from telegram_setup.newbot_backend import register_newbot_backend
from telegram_setup.newbot_gateway import NewBotGateway
from telegram_setup.newbot_store import OperatorApprovals
from telegram_setup.reader_gateway import ReaderGateway
from test_agent_gateway import RegistryFixture
from test_newbot_worker import Client, NAME, USERNAME, REQUEST
from test_sensitive_peer import TOKEN


class IntegrationTests(RegistryFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_registry(); self.data['agents']['a']['actions'].append('create-owned-bot'); self.registry.save(self.data)
        self.root = Path(self.temporary.name); self.client = Client(); self.mcp = FastMCP('synthetic backend')
        self.runtime = NS(mcp=self.mcp, ToolAnnotations=ToolAnnotations, with_account=lambda **_: lambda fn: fn,
                          get_client=lambda _: self.client)
        self.patch = patch('telegram_setup.newbot_backend.private_dir', return_value=self.root); self.patch.start()
        register_newbot_backend(self.runtime)
        class Bridge:
            available_tools = {'agent_request_owned_bot'}
            async def call(_, name, args): return await self.mcp._tool_manager.call_tool(name, args)
        self.api = ReaderGateway(self.registry, Bridge())
        self.gateway = NewBotGateway(self.api)

    def tearDown(self): self.patch.stop(); self.teardown_registry()

    async def test_gateway_to_backend_exact_approved_creation_and_replay(self):
        OperatorApprovals(self.root / 'newbot-approvals').issue('a', NAME, USERNAME, REQUEST)
        with patch('telegram_setup.newbot_worker.logging_is_quiet', return_value=True):
            result = await self.gateway.request('a', NAME, USERNAME, REQUEST)
            repeated = await self.gateway.request('a', NAME, USERNAME, REQUEST)
        self.assertEqual(result['status'], 'created_verified'); self.assertEqual(repeated, result)
        self.assertEqual(self.client.sent, ['/newbot', NAME, USERNAME])
        self.assertNotIn(TOKEN, json.dumps(result)); self.assertTrue(result['token_ref'].startswith('botcred_'))

    async def test_no_owner_assertion_or_scope_means_no_botfather_messages(self):
        with patch('telegram_setup.newbot_worker.logging_is_quiet', return_value=True):
            result = await self.gateway.request('a', NAME, USERNAME, REQUEST)
        self.assertEqual(result, {'error': 'newbot_request_unavailable'})
        self.assertEqual(self.client.sent, [])
        self.data['agents']['a']['actions'].remove('create-owned-bot'); self.registry.save(self.data)
        with self.assertRaises(PermissionError): await self.gateway.request('a', NAME, USERNAME, REQUEST)
        self.assertEqual(self.client.sent, [])

    async def test_newbot_tool_not_exposed_by_default(self):
        mcp = create_gateway(self.registry, 'a')
        self.assertNotIn('request_owned_bot', {t.name for t in await mcp.list_tools()})
        with self.assertRaises(ValueError): create_gateway(self.registry, None, enable_newbot=True)
        names = {t.name for t in await create_gateway(self.registry, 'a', enable_newbot=True).list_tools()}
        self.assertIn('request_owned_bot', names)

    async def test_delegated_profile_requires_explicit_mode_and_is_not_human_authentication(self):
        self.data['agents']['a']['bot_create_mode'] = 'delegated'; self.registry.save(self.data)
        with patch('telegram_setup.newbot_worker.logging_is_quiet', return_value=True):
            result = await self.gateway.request('a', NAME, USERNAME, REQUEST)
        self.assertEqual(result['status'], 'created_verified')
        self.assertEqual(result['authorization_mode'], 'delegated')
        self.assertFalse(result['owner_identity_verified'])
        self.assertFalse((self.root / 'newbot-approvals').exists())

    async def test_real_stdio_end_to_end_hides_token_and_rejects_spoofed_agent(self):
        OperatorApprovals(self.root / 'newbot-approvals').issue('a', NAME, USERNAME, REQUEST)
        script = Path(__file__).with_name('offline_newbot_gateway.py')
        params = StdioServerParameters(command=sys.executable, args=[str(script), str(self.root), str(self.registry.path)])
        with open(os.devnull, 'w') as err:
            async with stdio_client(params, errlog=err) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    args = {'name': NAME, 'username': USERNAME, 'request_id': REQUEST}
                    result = await session.call_tool('request_owned_bot', args)
                    self.assertFalse(result.isError); output = text_payload(result)
                    self.assertEqual(output['status'], 'created_verified')
                    self.assertNotIn(TOKEN, str(result))
                    repeated = await session.call_tool('request_owned_bot', args)
                    self.assertEqual(text_payload(repeated), output)
                    spoof = await session.call_tool('request_owned_bot', dict(args, agent_id='other'))
                    self.assertTrue(spoof.isError)
                    self.assertNotIn(TOKEN, str(spoof))
                    fake_approval = await session.call_tool('request_owned_bot', dict(args, approved=True))
                    self.assertTrue(fake_approval.isError)
                    self.assertNotIn(TOKEN, str(fake_approval))
