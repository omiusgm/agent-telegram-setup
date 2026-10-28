import asyncio
import contextlib
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from mcp.types import CallToolResult, TextContent

from telegram_setup.agents import READ_ACTIONS, AccessDenied, AgentRegistry
from telegram_setup.gateway import BearerGate, UpstreamBridge, create_gateway
from telegram_setup.reader_gateway import ReaderGateway

TOKEN_A = 'synthetic-reader-a-' + 'a' * 32
TOKEN_B = 'synthetic-reader-b-' + 'b' * 32


def row(token, chat_ids):
    return {'enabled': True, 'token_sha256': hashlib.sha256(token.encode()).hexdigest(),
            'expires_at': int(time.time()) + 3600, 'chat_ids': chat_ids,
            'contact_ids': [101], 'all_chats': False, 'all_contacts': False,
            'include_phones': False, 'actions': sorted(READ_ACTIONS)}


class FakeUpstream:
    def __init__(self):
        self.calls = []
        self.flood = False
        self.identity = os.getpid()

    async def call(self, name, args):
        self.calls.append((name, args))
        if self.flood:
            return {'error': 'synthetic_flood_wait', 'retry_after_seconds': 30}
        if name == 'get_me':
            return {'type': 'user', 'id': 123456, 'name': 'not exposed'}
        if name == 'get_chat':
            return {'id': args['chat_id'], 'title': 'Permitted fixture', 'phone': 'not exposed'}
        if name == 'get_chats':
            return {'results': [{'chat_id': 11, 'title': 'one'}, {'chat_id': 22, 'title': 'two'}]}
        if name == 'list_contacts':
            return {'results': [{'id': 101, 'name': 'Alice', 'phone': 'not exposed'},
                                {'id': 202, 'name': 'Bob', 'phone': 'not exposed'}]}
        if name == 'read_history_page':
            ids = [i for i in range(1, 6) if i > args['after_id'] and
                   (args.get('until_id') is None or i <= args['until_id'])]
            chosen = ids[:args['limit']]
            return {'chat_id': args['chat_id'], 'messages': [{'id': i, 'text': 'untrusted fixture'} for i in chosen],
                    'has_more': len(ids) > len(chosen),
                    'next_after_id': chosen[-1] if chosen else args['after_id']}
        if name in ('search_messages', 'get_messages'):
            if name == 'get_messages':
                assert set(args) == {'chat_id', 'page', 'page_size'}
            return {'results': [{'id': 1, 'text': 'fixture'}]}
        raise AssertionError('Unexpected upstream call: ' + name)


class RegistryFixture:
    def setup_registry(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.registry = AgentRegistry(Path(self.temporary.name) / 'agents.json')
        self.data = {'schema_version': 1, 'agents': {'a': row(TOKEN_A, [11]), 'b': row(TOKEN_B, [22])}}
        self.registry.save(self.data)
        self.upstream = FakeUpstream()

    def teardown_registry(self):
        self.temporary.cleanup()


class UpstreamFormatTests(unittest.IsolatedAsyncioTestCase):
    async def test_vendor_flood_wait_is_preserved_even_on_protocol_error(self):
        text = 'Rate limit exceeded (FloodWait): Telegram requires waiting 30 seconds before repeating this operation.'
        for error in (False, True):
            reply = CallToolResult(content=[TextContent(type='text', text=text)], isError=error)
            bridge = UpstreamBridge(SimpleNamespace(call_tool=AsyncMock(return_value=reply)))
            result = await bridge.call('read_history_page', {'chat_id': 11})
            self.assertEqual(result, {'error': 'telegram_cooldown', 'retry_after_seconds': 30})


class RegistryTests(RegistryFixture, unittest.TestCase):
    def setUp(self): self.setup_registry()
    def tearDown(self): self.teardown_registry()

    def test_independent_tokens_and_unknown_token(self):
        self.assertEqual(self.registry.authenticate(TOKEN_A), 'a')
        self.assertEqual(self.registry.authenticate(TOKEN_B), 'b')
        with self.assertRaises(AccessDenied): self.registry.authenticate('unknown-' + 'x' * 32)

    def test_revocation_and_expiry_checked_every_request(self):
        self.registry.authenticate(TOKEN_A)
        self.data['agents']['a']['enabled'] = False; self.registry.save(self.data)
        with self.assertRaises(AccessDenied): self.registry.authenticate(TOKEN_A)
        self.assertEqual(self.registry.authenticate(TOKEN_B), 'b')
        self.data['agents']['b']['expires_at'] = int(time.time()) - 1; self.registry.save(self.data)
        with self.assertRaises(AccessDenied): self.registry.profile('b')

    def test_unsafe_or_symlink_registry_fails_closed(self):
        self.registry.path.chmod(0o644)
        with self.assertRaises(AccessDenied): self.registry.load()
        self.registry.path.chmod(0o600)
        link = self.registry.path.with_name('link.json'); link.symlink_to(self.registry.path)
        with self.assertRaises(OSError): AgentRegistry(link).load()

    def test_unknown_write_capability_is_invalid(self):
        self.data['agents']['a']['actions'].append('send_message'); self.registry.save(self.data)
        with self.assertRaises(AccessDenied): self.registry.load()


class GatewayTests(RegistryFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_registry(); self.api = ReaderGateway(self.registry, self.upstream)
    def tearDown(self): self.teardown_registry()

    async def test_forbidden_chat_rejected_before_upstream(self):
        for op in [self.api.history('a', 22), self.api.search('a', 22, 'x'),
                   self.api.messages('a', 22, [1])]:
            with self.assertRaises(AccessDenied): await op
        self.assertEqual(self.upstream.calls, [])

    async def test_no_arbitrary_forwarding_or_write(self):
        for name in ('send_message', 'send_file', 'edit_message', 'delete_message', 'mark_as_read',
                     'join_owner_requested_source'):
            with self.assertRaises(PermissionError):
                await self.api.call('a', 'history', name, {'chat_id': 11}, 11)
        self.assertEqual(self.upstream.calls, [])

    async def test_list_filters_before_response_and_does_not_list_all_chats(self):
        result = await self.api.dialogs('a')
        self.assertEqual([x['id'] for x in result['chats']], [11])
        self.assertNotIn('phone', result['chats'][0])
        self.assertEqual(self.upstream.calls[0], ('get_chat', {'chat_id': 11}))

    async def test_contacts_filtered_and_phone_not_exposed(self):
        result = await self.api.contacts('a')
        self.assertEqual(result['contacts'], [{'id': 101, 'name': 'Alice'}])
        self.assertEqual((await self.api.contacts('a', query='Bob'))['contacts'], [])

    async def test_empty_contact_scope_does_not_call_upstream(self):
        self.data['agents']['a']['contact_ids'] = []; self.registry.save(self.data)
        self.assertEqual((await self.api.contacts('a'))['contacts'], [])
        self.assertEqual(self.upstream.calls, [])

    async def test_incremental_history_no_duplicates(self):
        first = await self.api.history('a', 11, limit=2)
        second = await self.api.history('a', 11, after_id=first['next_after_id'], limit=2)
        self.assertEqual([m['id'] for m in first['messages']], [1, 2])
        self.assertEqual([m['id'] for m in second['messages']], [3, 4])

    async def test_recent_messages_match_pinned_upstream_schema(self):
        await self.api.messages('a', 11, page=2, limit=15)
        self.assertEqual(self.upstream.calls, [('get_messages', {'chat_id': 11, 'page': 2, 'page_size': 15})])

    async def test_agent_b_scope_independent(self):
        self.assertTrue((await self.api.status('a'))['connected'])
        self.assertTrue((await self.api.status('b'))['connected'])
        self.data['agents']['a']['enabled'] = False; self.registry.save(self.data)
        with self.assertRaises(AccessDenied): await self.api.history('a', 11)
        self.assertEqual((await self.api.history('b', 22))['chat_id'], 22)

    async def test_account_cooldown_prevents_another_upstream_request(self):
        self.upstream.flood = True
        first = await self.api.history('a', 11)
        self.assertGreater(first['retry_after_seconds'], 0)
        before = len(self.upstream.calls)
        second = await self.api.history('b', 22)
        self.assertEqual(second['error'], 'telegram_cooldown')
        self.assertEqual(len(self.upstream.calls), before)

    async def test_revocation_of_queued_work_is_rechecked(self):
        await self.api.lock.acquire()
        task = asyncio.create_task(self.api.history('a', 11))
        await asyncio.sleep(0)
        self.data['agents']['a']['enabled'] = False; self.registry.save(self.data)
        self.api.lock.release()
        with self.assertRaises(AccessDenied): await task
        self.assertEqual(self.upstream.calls, [])

    async def test_http_auth_rejects_missing_duplicate_and_revoked_tokens(self):
        seen = []
        async def app(scope, receive, send): seen.append(scope['telegram_agent_id'])
        gate = BearerGate(app, self.registry)
        async def receive(): return {'type': 'http.disconnect'}
        async def check(headers):
            output = []
            async def send(message): output.append(message)
            await gate({'type': 'http', 'headers': headers}, receive, send)
            return output
        self.assertEqual((await check([]))[0]['status'], 401)
        h = (b'authorization', ('Bearer ' + TOKEN_A).encode())
        self.assertEqual((await check([h, h]))[0]['status'], 401)
        self.assertEqual(await check([h]), [])
        self.assertEqual(seen, ['a'])
        self.data['agents']['a']['enabled'] = False; self.registry.save(self.data)
        self.assertEqual((await check([h]))[0]['status'], 401)

    async def test_catalog_contains_only_reader_tools(self):
        @contextlib.asynccontextmanager
        async def fake_connection(): yield self.upstream
        server = create_gateway(self.registry, 'a', fake_connection)
        catalog = await server.list_tools()
        self.assertEqual({t.name for t in catalog}, {'telegram_status', 'list_chats', 'list_contacts',
            'read_history_page', 'search_messages', 'get_messages'})
        self.assertTrue(all(t.annotations.readOnlyHint for t in catalog))


if __name__ == '__main__':
    unittest.main()
