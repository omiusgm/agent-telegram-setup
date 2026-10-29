"""Synthetic-only regression checks for the restricted cloud facade."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, TextContent
from telethon.tl import types

from telegram_setup.account_search import AccountSearchBackend
from telegram_setup.attachment_gateway import AttachmentGateway
from telegram_setup.client import text_payload
from telegram_setup.gateway import UpstreamBridge
from telegram_setup.peer_gateway import PeerGateway
from telegram_setup.photo_gateway import PhotoMixin
from telegram_setup.poll_gateway import PollGateway
from telegram_setup.reader_gateway import ReaderGateway
from telegram_setup.sensitive_peer import BOTFATHER_ID, redact_tokens, sanitize_account_search
from test_account_search import FakeMTProto, converter, dialog, fake_runtime, message, page
from test_agent_gateway import RegistryFixture
from test_scoped_search import SearchUpstream

# Deliberately synthetic and unrelated to any Telegram account.
TOKEN = '123456789:' + 'A' * 35


class GuardTests(RegistryFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_registry()
        self.data['agents']['a']['all_chats'] = True
        self.registry.save(self.data)

    def tearDown(self): self.teardown_registry()

    async def test_all_direct_read_surfaces_deny_before_backend(self):
        api = ReaderGateway(self.registry, self.upstream)
        for operation in (api.history('a', BOTFATHER_ID), api.search('a', BOTFATHER_ID, 'x'),
                          api.messages('a', BOTFATHER_ID)):
            with self.assertRaisesRegex(PermissionError, 'sensitive_chat_not_available'):
                await operation
        self.assertEqual(self.upstream.calls, [])
        session = SimpleNamespace(call_tool=AsyncMock())
        bridge = UpstreamBridge(session)
        for name, args in (('read_history_page', {'chat_id': BOTFATHER_ID}),
                           ('get_messages', {'chat_id': str(BOTFATHER_ID)}),
                           ('download_agent_attachment', {'peer': '@BotFather'}),
                           ('read_agent_peer_profile', {'peer': 'BOTFATHER'}),
                           ('agent_read_poll', {'chat_id': BOTFATHER_ID})):
            with self.assertRaises(PermissionError): await bridge.call(name, args)
        session.call_tool.assert_not_awaited()

    async def test_old_global_search_response_and_nested_secrets_filtered(self):
        payload = {'messages': [{'chat_id': BOTFATHER_ID, 'id': 1, 'text': 'not a token'},
                                {'chat_id': 11, 'id': 2, 'text': 'https://api.telegram.org/bot' + TOKEN}],
                   'verified_chat_ids': [BOTFATHER_ID, 11], 'omitted_records': 0,
                   'search_engine': 'account_global_v2'}
        reply = CallToolResult(content=[TextContent(type='text', text=json.dumps(payload))])
        bridge = UpstreamBridge(SimpleNamespace(call_tool=AsyncMock(return_value=reply)))
        result = await bridge.call('read_account_search_page', {'agent_id': 'a', 'query': 'fixture'})
        self.assertEqual([x['chat_id'] for x in result['messages']], [11])
        self.assertEqual(result['verified_chat_ids'], [11])
        self.assertNotIn(TOKEN, json.dumps(result)); self.assertEqual(result['omitted_records'], 1)
        self.assertIn('[REDACTED_BOT_TOKEN]', result['messages'][0]['text'])

    async def test_unknown_global_shape_fails_closed_and_errors_do_not_leak(self):
        with self.assertRaises(RuntimeError): sanitize_account_search({'messages': [{'id': 1}], 'verified_chat_ids': []})
        reply = CallToolResult(content=[TextContent(type='text', text=json.dumps(
            {'error': 'error ' + TOKEN, 'retry_after_seconds': 3}))])
        result = await UpstreamBridge(SimpleNamespace(call_tool=AsyncMock(return_value=reply))).call('get_me', {})
        self.assertNotIn(TOKEN, json.dumps(result))

    async def test_new_backend_global_search_never_caches_botfather_contents(self):
        bot = types.User(id=BOTFATHER_ID, access_hash=1, first_name='fixture')
        peer = types.PeerUser(BOTFATHER_ID)
        user = types.User(id=11, access_hash=11, first_name='other')
        fixture = page([message(peer, 7, TOKEN), message(types.PeerUser(11), 4, TOKEN)],
                       users=[bot, user], dialogs=[dialog(peer), dialog(types.PeerUser(11))])
        client = FakeMTProto([fixture])
        folder = Path(self.temporary.name) / 'account'
        api = AccountSearchBackend(fake_runtime(client), self.registry, folder, converter)
        result = await api.page('a', 'test')
        self.assertEqual([r['chat_id'] for r in result['messages']], [11])
        self.assertNotIn(TOKEN, json.dumps(result)); self.assertEqual(result['omitted_records'], 1)
        for path in folder.glob('*.json'):
            self.assertNotIn(TOKEN, path.read_text())

    async def test_restricted_search_skips_sensitive_chat_without_read(self):
        self.data['agents']['a']['all_chats'] = False
        self.data['agents']['a']['chat_ids'] = [11, BOTFATHER_ID]
        self.registry.save(self.data)
        upstream = SearchUpstream()
        api = ReaderGateway(self.registry, upstream, search_cursor_dir=Path(self.temporary.name) / 'restricted')
        result = await api.search_allowed('a', 'synthetic', limit=50)
        self.assertTrue(result['complete'])
        self.assertEqual([x[1]['chat_id'] for x in upstream.calls], [11])

    async def test_staged_files_and_photos_are_rechecked(self):
        store = SimpleNamespace(load=lambda *a, **k: ({'chat_id': BOTFATHER_ID, 'peer_id': BOTFATHER_ID}, b'data'))
        api = ReaderGateway(self.registry, self.upstream)
        with self.assertRaises(PermissionError): AttachmentGateway(api, store).original_bytes('a', 'fixture')
        with self.assertRaises(PermissionError): PeerGateway(api, store).original_bytes('a', 'fixture')
        photo = PhotoMixin(); photo.registry = self.registry
        with self.assertRaises(PermissionError): photo.photo_policy('a', BOTFATHER_ID, 1)

    async def test_poll_and_profile_guard_before_backend(self):
        api = ReaderGateway(self.registry, self.upstream)
        poll = PollGateway(api, self.temporary.name)
        with self.assertRaises(PermissionError): poll._policy('a', 'read-polls', BOTFATHER_ID, 11, write=False)
        with self.assertRaises(PermissionError): await PeerGateway(api, None).call('a', 'read-profiles', 'read_agent_peer_profile', '@BoTfAtHeR')
        self.assertEqual(self.upstream.calls, [])

    def test_redacts_nested_strings_and_bot_api_urls_without_breaking_other_data(self):
        value = {'text': 'a ' + TOKEN, 'nested': [{'url': 'https://api.telegram.org/bot' + TOKEN + '/getMe'}], 'id': 123}
        safe = redact_tokens(value)
        self.assertNotIn(TOKEN, json.dumps(safe)); self.assertEqual(safe['id'], 123)
        self.assertIn('[REDACTED_BOT_TOKEN]', safe['nested'][0]['url'])

    async def test_actual_stdio_gateway_denies_sensitive_chat_and_sanitizes_global_search(self):
        script = Path(__file__).with_name('offline_sensitive_gateway.py')
        params = StdioServerParameters(command=sys.executable, args=[str(script), str(self.registry.path)])
        with open(os.devnull, 'w') as err:
            async with stdio_client(params, errlog=err) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()
                    status = text_payload(await client.call_tool('telegram_status', {}))
                    self.assertTrue(status['sensitive_botfather_excluded'])
                    denied = await client.call_tool('read_history_page', {'chat_id': BOTFATHER_ID})
                    self.assertTrue(denied.isError)
                    self.assertNotIn(TOKEN, str(denied))
                    history = text_payload(await client.call_tool('read_history_page', {'chat_id': 11}))
                    self.assertEqual(history['messages'][0]['text'], 'ordinary fixture')
                    result = await client.call_tool('search_allowed_messages', {'query': 'synthetic'})
                    self.assertFalse(result.isError)
                    safe = text_payload(result)
                    self.assertEqual([row['chat_id'] for row in safe['messages']], [11])
                    self.assertNotIn(TOKEN, json.dumps(safe))
                    self.assertIn('[REDACTED_BOT_TOKEN]', safe['messages'][0]['text'])
