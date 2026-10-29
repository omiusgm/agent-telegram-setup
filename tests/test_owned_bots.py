"""Synthetic owner-only bot metadata and edit semantics; no live BotFather."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from telethon.tl import functions, types

from telegram_setup.agents import AccessDenied
from telegram_setup.client import text_payload
from telegram_setup.gateway import create_gateway
from telegram_setup.owned_bots import OwnedBots, validate_profile
from telegram_setup.permissions import save_settings
from test_agent_gateway import RegistryFixture
from test_sensitive_peer import TOKEN


class BotClient:
    def __init__(self):
        self.bot = types.User(id=123, access_hash=1234, bot=True, first_name='Original', username='fixture_bot')
        self.info = {'name': 'Original', 'about': 'Initial', 'description': 'Synthetic'}
        self.calls = []
        self.fail_after_write = False

    async def __call__(self, request, **kwargs):
        assert kwargs == {'flood_sleep_threshold': 0}
        self.calls.append(request)
        if isinstance(request, functions.bots.GetAdminedBotsRequest): return [self.bot]
        if isinstance(request, functions.bots.GetBotInfoRequest):
            if self.fail_after_write: raise RuntimeError('provider details must not escape')
            return SimpleNamespace(**self.info)
        if isinstance(request, functions.bots.SetBotInfoRequest):
            for field in self.info:
                value = getattr(request, field, None)
                if value is not None: self.info[field] = value
            return True
        raise AssertionError('unexpected Telegram operation')


class OwnedBotTests(RegistryFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_registry()
        self.data['agents']['a']['actions'] += ['read-owned-bots', 'edit-owned-bot-profile']
        self.data['agents']['a']['bot_profile_write_ids'] = [123]
        self.registry.save(self.data)
        self.client = BotClient()
        self.folder = Path(self.temporary.name) / 'bot-receipts'
        self.api = OwnedBots(self.client, self.registry, self.folder)

    def tearDown(self): self.teardown_registry()

    async def test_metadata_is_owned_and_does_not_expose_access_hash(self):
        result = await self.api.list('a')
        self.assertEqual(result['bots'][0]['bot_id'], 123)
        self.assertNotIn('1234', json.dumps(result))
        profile = await self.api.get('a', 123)
        self.assertEqual((profile['name'], profile['description']), ('Original', 'Synthetic'))
        with self.assertRaises(AccessDenied): await self.api.get('a', 456)

    async def test_read_permission_and_exact_edit_target_are_independent(self):
        with self.assertRaises(AccessDenied): await self.api.edit('a', 456, 'fixture-0001', {'about': 'New'})
        self.assertEqual(self.client.calls, [])
        self.data['agents']['a']['actions'].remove('read-owned-bots')
        self.registry.save(self.data)
        with self.assertRaises(AccessDenied): await self.api.list('a')

    async def test_edit_rechecks_ownership_and_readback_and_is_idempotent(self):
        result = await self.api.edit('a', 123, 'fixture-0001', {'about': 'New about', 'description': ''})
        self.assertEqual(result['status'], 'delivered_verified')
        self.assertTrue(result['readback_verified'])
        count = len([x for x in self.client.calls if isinstance(x, functions.bots.SetBotInfoRequest)])
        repeated = await self.api.edit('a', 123, 'fixture-0001', {'about': 'New about', 'description': ''})
        self.assertEqual(repeated['status'], 'delivered_verified')
        self.assertEqual(len([x for x in self.client.calls if isinstance(x, functions.bots.SetBotInfoRequest)]), count)
        with self.assertRaises(ValueError): await self.api.edit('a', 123, 'fixture-0001', {'about': 'Different'})
        for file in self.folder.glob('*.json'):
            self.assertNotIn('New about', file.read_text())

    async def test_unknown_after_dispatch_never_repeats_with_another_id(self):
        self.client.fail_after_write = True
        first = await self.api.edit('a', 123, 'fixture-0001', {'name': 'New name'})
        second = await self.api.edit('a', 123, 'fixture-0002', {'name': 'New name'})
        self.assertEqual((first['status'], second['status']), ('uncertain_do_not_retry',) * 2)
        self.assertEqual(len([x for x in self.client.calls if isinstance(x, functions.bots.SetBotInfoRequest)]), 1)
        self.assertNotIn('provider details', json.dumps(first))

    async def test_owned_bot_lost_before_change_is_never_mutated(self):
        self.client.bot.id = 777
        with self.assertRaises(AccessDenied): await self.api.edit('a', 123, 'fixture-0001', {'name': 'New'})
        self.assertFalse(any(isinstance(x, functions.bots.SetBotInfoRequest) for x in self.client.calls))

    def test_profile_validation_never_accepts_token_or_unknown_field(self):
        for value in ({'description': TOKEN}, {'name': ''}, {'foo': 'bar'}, {}, {'about': 'a' * 121}):
            with self.assertRaises(ValueError): validate_profile(value)

    async def test_default_facade_does_not_publish_bot_tools(self):
        mcp = create_gateway(self.registry, 'a')
        names = {t.name for t in await mcp.list_tools()}
        self.assertFalse(names & {'list_owned_bots', 'get_owned_bot_profile', 'update_owned_bot_profile'})
        with self.assertRaises(ValueError): create_gateway(self.registry, None, enable_owned_bots=True)

    async def test_stdio_facade_real_schema_and_target_scope_with_fake_telegram(self):
        script = Path(__file__).with_name('offline_owned_bots.py')
        params = StdioServerParameters(command=sys.executable, args=[str(script), str(self.registry.path), str(self.folder)])
        with open(os.devnull, 'w') as err:
            async with stdio_client(params, errlog=err) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize(); catalog = {t.name: t for t in (await session.list_tools()).tools}
                    self.assertTrue({'list_owned_bots', 'get_owned_bot_profile', 'update_owned_bot_profile'} <= catalog.keys())
                    self.assertTrue(catalog['list_owned_bots'].annotations.readOnlyHint)
                    self.assertFalse(catalog['update_owned_bot_profile'].annotations.readOnlyHint)
                    listing = text_payload(await session.call_tool('list_owned_bots', {}))
                    self.assertEqual(listing['bots'][0]['bot_id'], 123)
                    profile = text_payload(await session.call_tool('get_owned_bot_profile', {'bot_id': 123}))
                    self.assertEqual(profile['name'], 'Original')
                    args = {'bot_id': 123, 'request_id': 'bot-stdio-001', 'about': 'Updated'}
                    update = text_payload(await session.call_tool('update_owned_bot_profile', args))
                    self.assertEqual(update['status'], 'delivered_verified')
                    self.assertTrue((await session.call_tool('update_owned_bot_profile', dict(args, agent_id='b'))).isError)
                    self.assertTrue((await session.call_tool('update_owned_bot_profile', dict(args, bot_id=456))).isError)

    async def test_internal_backend_permissions_disabled_by_default(self):
        root = Path(__file__).resolve().parents[1]
        if not (root / '.upstream' / 'main.py').is_file(): self.skipTest('pinned upstream not staged')
        for selected in (['read'], ['read', 'agent-bot-read'], ['read', 'agent-bot-read', 'agent-bot-edit'],
                         ['read', 'agent-bot-create'], ['read', 'agent-bot-notify']):
            with tempfile.TemporaryDirectory() as home:
                with patch.dict(os.environ, {'AGENT_TELEGRAM_HOME': home}):
                    save_settings({'schema_version': 1, 'enabled': selected, 'transcription_engine': None})
                params = StdioServerParameters(command=sys.executable, args=[str(root / 'tests/offline_server.py')],
                                               cwd=str(root), env={'AGENT_TELEGRAM_HOME': home})
                with open(os.devnull, 'w') as err:
                    async with stdio_client(params, errlog=err) as (reader, writer):
                        async with ClientSession(reader, writer) as session:
                            await session.initialize(); names = {t.name for t in (await session.list_tools()).tools}
                            self.assertEqual('agent_list_owned_bots' in names, 'agent-bot-read' in selected)
                            self.assertEqual('agent_get_owned_bot_profile' in names, 'agent-bot-read' in selected)
                            self.assertEqual('agent_update_owned_bot_profile' in names, 'agent-bot-edit' in selected)
                            self.assertEqual('agent_request_owned_bot' in names, 'agent-bot-create' in selected)
                            self.assertEqual('agent_notify_owner_via_bot' in names, 'agent-bot-notify' in selected)
