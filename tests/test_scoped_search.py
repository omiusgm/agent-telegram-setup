"""Scoped fan-out, archive coverage, resumability and post-read revocation."""
import json
from pathlib import Path
import tempfile
import unittest

from telegram_setup.agents import AccessDenied
from telegram_setup.reader_gateway import ReaderGateway
from telegram_setup.scoped_search import SearchCursors
from test_agent_gateway import RegistryFixture, FakeUpstream


class SearchUpstream(FakeUpstream):
    def __init__(self):
        super().__init__()
        self.rows = {11: [9, 7, 5], 22: [8], 33: [6]}
        self.fail_chat = None
        self.after_read = None
        self.spoof = False

    async def call(self, name, args):
        self.calls.append((name, dict(args)))
        if name == 'read_dialog_page':
            if args['archived']:
                return {'chats': [{'id': 33}], 'next_cursor': None}
            return {'chats': [{'id': 11}, {'id': 22}], 'next_cursor': None}
        if name == 'read_search_page':
            if self.flood:
                return {'error': 'telegram_cooldown', 'retry_after_seconds': 15}
            if args['chat_id'] == self.fail_chat:
                return {'error': 'upstream_failed'}
            until = 10 if args['until_id'] is None else args['until_id']
            ids = [i for i in self.rows.get(args['chat_id'], []) if i <= until
                   and (not args['before_id'] or i < args['before_id'])]
            chosen = ids[:args['limit']]
            if self.after_read:
                self.after_read()
            return {'chat_id': args['chat_id'], 'until_id': until,
                    'messages': [{'id': i, 'chat_id': 999 if self.spoof else args['chat_id'],
                                  'text': 'synthetic'} for i in chosen],
                    'has_more': len(ids) > len(chosen),
                    'next_before_id': chosen[-1] if len(ids) > len(chosen) else None}
        if name == 'search_community_metadata':
            return {'communities': [{'id': -10011, 'type': 'group', 'username': 'fixture_group',
                                     'title': 'Public fixture', 'phone': 'never return', 'url': 'https://bad.test'},
                                    {'id': 22, 'type': 'user', 'username': 'fixture_user'},
                                    {'id': -10022, 'type': 'channel', 'username': 'fixture_channel'}]}
        raise AssertionError('forbidden raw/global search call: ' + name)


class ScopedSearchTests(RegistryFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_registry()
        self.upstream = SearchUpstream()
        self.folder = Path(self.temporary.name) / 'cursors'
        self.api = ReaderGateway(self.registry, self.upstream, search_cursor_dir=self.folder)

    def tearDown(self): self.teardown_registry()

    async def test_restricted_only_permitted_id_before_rpc_and_pagination_without_prefix_refetch(self):
        result = await self.api.search_allowed('a', 'fixture', limit=2, request_budget=1)
        self.assertEqual([r['id'] for r in result['messages']], [9, 7])
        self.assertFalse(result['complete'])
        cursor = result['next_cursor']
        next_page = await self.api.search_allowed('a', 'fixture', cursor, limit=2, request_budget=1)
        self.assertEqual([r['id'] for r in next_page['messages']], [5])
        self.assertTrue(next_page['complete'])
        self.assertEqual([c[0] for c in self.upstream.calls], ['read_search_page'] * 2)
        self.assertTrue(all(c[1]['chat_id'] == 11 for c in self.upstream.calls))
        self.assertEqual(self.upstream.calls[1][1]['before_id'], 7)
        before = len(self.upstream.calls)
        restarted = ReaderGateway(self.registry, self.upstream, search_cursor_dir=self.folder)
        self.assertEqual(await restarted.search_allowed('a', 'fixture', cursor, 2, 1), next_page)
        self.assertEqual(len(self.upstream.calls), before)

    async def test_restricted_many_ids_budget_is_not_coverage_cap(self):
        self.data['agents']['a']['chat_ids'] = [11, 22, 33]; self.registry.save(self.data)
        cursor, seen, pages = None, [], 0
        while True:
            result = await self.api.search_allowed('a', 'fixture', cursor, limit=2, request_budget=1)
            seen.extend((r['chat_id'], r['id']) for r in result['messages']); pages += 1
            cursor = result['next_cursor']
            if not cursor: break
            self.assertLess(pages, 12)
        self.assertTrue(result['complete'])
        self.assertIn((33, 6), seen)
        self.assertEqual(len(seen), 5)
        self.assertTrue(all(n == 'read_search_page' for n, _ in self.upstream.calls))

    async def test_scope_query_size_agent_expiry_binding_and_revocation_after_response(self):
        result = await self.api.search_allowed('a', 'fixture', limit=1, request_budget=1)
        for agent, query, size in [('b', 'fixture', 1), ('a', 'other', 1), ('a', 'fixture', 2)]:
            with self.assertRaises(AccessDenied):
                await self.api.search_allowed(agent, query, result['next_cursor'], size, 1)
        self.data['agents']['a']['chat_ids'] = [22]; self.registry.save(self.data)
        before = len(self.upstream.calls)
        with self.assertRaises(AccessDenied):
            await self.api.search_allowed('a', 'fixture', result['next_cursor'], 1, 1)
        self.assertEqual(len(self.upstream.calls), before)
        self.data['agents']['a']['chat_ids'] = [11]; self.registry.save(self.data)
        def revoke():
            self.data['agents']['a']['enabled'] = False; self.registry.save(self.data)
        self.upstream.after_read = revoke
        with self.assertRaises(AccessDenied):
            await self.api.search_allowed('a', 'fixture')

    async def test_flood_wait_retains_same_failed_operation(self):
        self.upstream.flood = True
        first = await self.api.search_allowed('a', 'fixture', limit=1, request_budget=1)
        self.assertEqual(first['retry_after_seconds'], 16)
        self.assertEqual(first['chats_completed'], 0)
        count = len(self.upstream.calls)
        second = await self.api.search_allowed('a', 'fixture', first['next_cursor'], 1, 1)
        self.assertEqual(len(self.upstream.calls), count)
        self.api.retry_at = 0; self.upstream.flood = False
        resumed = await self.api.search_allowed('a', 'fixture', second['next_cursor'], 1, 1)
        self.assertEqual(resumed['messages'][0]['id'], 9)

    async def test_error_is_partial_and_spoofed_result_never_escapes(self):
        self.upstream.fail_chat = 11
        result = await self.api.search_allowed('a', 'fixture')
        self.assertTrue(result['scan_finished']); self.assertFalse(result['complete'])
        self.assertEqual(result['errors'], [{'chat_id': 11, 'error': 'search_failed'}])
        self.upstream.fail_chat = None; self.upstream.spoof = True
        with self.assertRaisesRegex(RuntimeError, 'identity'):
            await self.api.search_allowed('a', 'fixture')

    async def test_empty_scope_has_no_calls_and_public_is_independent_explicit_metadata_only(self):
        self.data['agents']['a']['chat_ids'] = []; self.registry.save(self.data)
        self.assertTrue((await self.api.search_allowed('a', 'fixture'))['complete'])
        self.assertEqual(self.upstream.calls, [])
        with self.assertRaises(AccessDenied): await self.api.public_communities('a', 'fixture')
        self.data['agents']['a']['actions'].append('public-search'); self.registry.save(self.data)
        result = await self.api.public_communities('a', 'fixture')
        self.assertEqual(len(result['communities']), 2)
        self.assertNotIn('phone', json.dumps(result))
        self.assertFalse(result['joined']); self.assertFalse(result['history_read'])
        self.assertTrue(all(r['url'].startswith('https://t.me/') for r in result['communities']))
        self.assertEqual(self.upstream.calls[0][0], 'search_community_metadata')
