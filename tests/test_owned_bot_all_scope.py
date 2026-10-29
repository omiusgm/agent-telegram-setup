"""All-owned scope is a separate opt-in; Telegram ownership remains mandatory."""
import contextlib
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from telethon.tl import functions

from telegram_setup import agents
from telegram_setup.agents import AccessDenied
from telegram_setup.client import text_payload
from telegram_setup.owned_bots import OwnedBots
from test_agent_gateway import RegistryFixture
from test_owned_bots import BotClient


class AllOwnedScopeTests(RegistryFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_registry()
        self.data['agents']['a']['actions'] += ['read-owned-bots', 'edit-owned-bot-profile']
        self.data['agents']['a']['bot_profile_write_ids'] = []
        self.registry.save(self.data)
        self.client = BotClient()
        self.api = OwnedBots(self.client, self.registry, Path(self.temporary.name) / 'bot-receipts')

    def tearDown(self): self.teardown_registry()

    def cli(self, action, agent='a', *extra):
        args = ['agents', '--registry', str(self.registry.path), action, agent, *extra]
        output = io.StringIO()
        with patch.object(sys, 'argv', args), contextlib.redirect_stdout(output): agents.main()
        return output.getvalue()

    def enable_all(self):
        self.data['agents']['a']['bot_profile_all_owned'] = True
        self.registry.save(self.data)

    async def test_default_off_and_all_chats_does_not_grant_profile_edits(self):
        self.data['agents']['a']['all_chats'] = True; self.registry.save(self.data)
        with self.assertRaises(AccessDenied): await self.api.edit('a', 123, 'all-scope-001', {'about': 'fixture'})
        self.assertEqual(self.client.calls, [])

    async def test_all_scope_edits_owned_and_rejects_unowned_before_mutation(self):
        self.enable_all()  # all_chats remains false; bot ownership is separate.
        result = await self.api.edit('a', 123, 'all-scope-001', {'about': 'fixture'})
        self.assertEqual(result['status'], 'delivered_verified')
        writes = sum(isinstance(x, functions.bots.SetBotInfoRequest) for x in self.client.calls)
        with self.assertRaises(AccessDenied): await self.api.edit('a', 456, 'all-scope-002', {'about': 'fixture'})
        self.assertEqual(sum(isinstance(x, functions.bots.SetBotInfoRequest) for x in self.client.calls), writes)
        self.assertTrue(any(isinstance(x, functions.bots.GetAdminedBotsRequest) for x in self.client.calls))

    async def test_ownership_is_fresh_for_each_new_write(self):
        self.enable_all()
        await self.api.edit('a', 123, 'all-scope-001', {'about': 'first'})
        self.client.bot.id = 777  # Telegram no longer reports bot 123 as owned.
        with self.assertRaises(AccessDenied): await self.api.edit('a', 123, 'all-scope-002', {'about': 'second'})
        self.assertEqual(sum(isinstance(x, functions.bots.SetBotInfoRequest) for x in self.client.calls), 1)

    async def test_revocation_during_ownership_lookup_blocks_the_write(self):
        self.enable_all(); original = self.client.__class__.__call__
        outer = self
        class RevokingClient(BotClient):
            async def __call__(inner, request, **kwargs):
                result = await original(inner, request, **kwargs)
                if isinstance(request, functions.bots.GetAdminedBotsRequest):
                    outer.data['agents']['a'].pop('bot_profile_all_owned', None)
                    outer.registry.save(outer.data)
                return result
        self.client = RevokingClient(); self.api.client = self.client
        with self.assertRaises(AccessDenied): await self.api.edit('a', 123, 'all-scope-001', {'about': 'first'})
        self.assertFalse(any(isinstance(x, functions.bots.SetBotInfoRequest) for x in self.client.calls))

    def test_grant_and_revoke_are_per_agent_and_preserve_exact_ids(self):
        self.data['agents']['a']['actions'].remove('edit-owned-bot-profile'); self.registry.save(self.data)
        self.cli('grant-bot-profile-all')
        profile = self.registry.profile('a')
        self.assertTrue(profile['bot_profile_all_owned']); self.assertIn('edit-owned-bot-profile', profile['actions'])
        self.assertNotIn('bot_profile_all_owned', self.registry.profile('b'))
        self.cli('grant-bot-profile', 'a', '--bot-id', '123')
        self.cli('revoke-bot-profile-all')
        profile = self.registry.profile('a')
        self.assertNotIn('bot_profile_all_owned', profile)
        self.assertEqual(profile['bot_profile_write_ids'], [123])
        self.assertIn('edit-owned-bot-profile', profile['actions'])
        self.cli('revoke-bot-profile', 'a', '--bot-id', '123')
        self.assertNotIn('edit-owned-bot-profile', self.registry.profile('a')['actions'])

    def test_revoking_exact_id_does_not_silently_remove_broad_scope(self):
        self.cli('grant-bot-profile-all'); self.cli('grant-bot-profile', 'a', '--bot-id', '123')
        message = self.cli('revoke-bot-profile', 'a', '--bot-id', '123')
        profile = self.registry.profile('a')
        self.assertTrue(profile['bot_profile_all_owned']); self.assertIn('edit-owned-bot-profile', profile['actions'])
        self.assertIn('all-owned bot profile access remains active', message)
        self.cli('revoke-bot-profile-all')
        self.assertNotIn('edit-owned-bot-profile', self.registry.profile('a')['actions'])

    def test_invalid_flag_types_fail_closed_and_revoking_agent_clears_broad_scope(self):
        for value in (0, 1, 'true', None):
            self.data['agents']['a']['bot_profile_all_owned'] = value; self.registry.save(self.data)
            with self.assertRaises(AccessDenied): self.registry.load()
        self.data['agents']['a']['bot_profile_all_owned'] = True; self.registry.save(self.data)
        self.cli('revoke', 'a')
        row = self.registry.load()['agents']['a']
        self.assertFalse(row['enabled']); self.assertNotIn('bot_profile_all_owned', row)

    async def test_real_stdio_reports_scope_and_rechecks_revocation(self):
        self.enable_all()
        script = Path(__file__).with_name('offline_owned_bots.py')
        params = StdioServerParameters(command=sys.executable,
            args=[str(script), str(self.registry.path), str(Path(self.temporary.name) / 'stdio-receipts')])
        with open(os.devnull, 'w') as err:
            async with stdio_client(params, errlog=err) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    status = text_payload(await session.call_tool('telegram_status', {}))
                    self.assertTrue(status['owned_bot_edit_all_owned']); self.assertFalse(status['read_only'])
                    args = {'bot_id': 123, 'request_id': 'all-stdio-001', 'about': 'fixture'}
                    result = await session.call_tool('update_owned_bot_profile', args)
                    self.assertFalse(result.isError); self.assertEqual(text_payload(result)['status'], 'delivered_verified')
                    denied = await session.call_tool('update_owned_bot_profile', dict(args, bot_id=456, request_id='all-stdio-002'))
                    self.assertTrue(denied.isError)
                    self.data['agents']['a'].pop('bot_profile_all_owned'); self.registry.save(self.data)
                    status = text_payload(await session.call_tool('telegram_status', {}))
                    self.assertFalse(status['owned_bot_edit_all_owned']); self.assertTrue(status['read_only'])
                    denied = await session.call_tool('update_owned_bot_profile', dict(args, request_id='all-stdio-003'))
                    self.assertTrue(denied.isError)
