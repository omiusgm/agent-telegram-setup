"""Synthetic MTProto pages: real request types, permissions, offsets and private cursor state."""
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest

from telethon import utils
from telethon.errors import FloodWaitError
from telethon.tl import functions, types

from telegram_setup.account_search import AccountSearchBackend
from telegram_setup.agents import AccessDenied
from telegram_setup.reader_gateway import ReaderGateway
from test_agent_gateway import RegistryFixture

DATE = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def message(peer, ident, text='synthetic match'):
    return NS(peer_id=peer, id=ident, date=DATE, message=text)


def dialog(peer, *, archived=False, top=999):
    return types.Dialog(peer=peer, top_message=top, read_inbox_max_id=0, read_outbox_max_id=0,
        unread_count=0, unread_mentions_count=0, unread_reactions_count=0, unread_poll_votes_count=0,
        notify_settings=types.PeerNotifySettings(), folder_id=1 if archived else None)


USER_A = types.User(id=11, access_hash=1100, first_name='Synthetic main', phone='not-exposed')
USER_B = types.User(id=22, access_hash=2200, first_name='Synthetic archived')
PEER_A, PEER_B = types.PeerUser(11), types.PeerUser(22)


def page(rows, *, sliced=False, rate=None, dialogs=None, users=None, chats=None, inexact=False):
    fields = {'messages': rows, 'topics': [], 'users': users or [USER_A, USER_B], 'chats': chats or []}
    result = types.messages.MessagesSlice(count=100, next_rate=rate, inexact=inexact, **fields) if sliced else types.messages.Messages(**fields)
    return result, dialogs if dialogs is not None else [dialog(PEER_A), dialog(PEER_B, archived=True)]


class FakeMTProto:
    def __init__(self, pages=None):
        self.pages = pages or [page([message(PEER_A, 9), message(PEER_B, 9)])]
        self.calls, self.index, self.active = [], 0, None
        self.flood = False; self.after_rpc = None

    async def __call__(self, request, **kwargs):
        self.calls.append(request)
        assert kwargs == {'flood_sleep_threshold': 0}
        if self.flood:
            raise FloodWaitError(request=None, capture=9)
        if isinstance(request, functions.messages.SearchGlobalRequest):
            assert request.broadcasts_only is None and request.folder_id is None
            self.active = self.pages[min(self.index, len(self.pages) - 1)]
            self.index += 1
            result = self.active[0]
        elif isinstance(request, functions.messages.GetPeerDialogsRequest):
            result = NS(dialogs=self.active[1])
        else:
            raise AssertionError('Unexpected whole-catalog/contact/post/mutation request')
        if self.after_rpc: self.after_rpc(request)
        return result


def fake_runtime(client):
    return NS(get_client=lambda _: client, is_chat_allowlist_enabled=lambda: False,
              sanitize_name=lambda text: text, person_name=lambda entity: getattr(entity, 'first_name', ''))


def converter(msg, *_):
    return {'id': msg.id, 'text': msg.message, 'date': msg.date}


class AccountBackendTests(RegistryFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_registry(); self.client = FakeMTProto(); self.rt = fake_runtime(self.client)
        self.folder = Path(self.temporary.name) / 'account-cursors'
        self.api = AccountSearchBackend(self.rt, self.registry, self.folder, converter)

    def tearDown(self): self.teardown_registry()

    def full(self):
        self.data['agents']['a']['all_chats'] = True; self.registry.save(self.data)

    async def test_restricted_or_backend_allowlist_denies_before_any_rpc_or_cursor(self):
        with self.assertRaises(AccessDenied): await self.api.page('a', 'fixture')
        self.full(); self.rt.is_chat_allowlist_enabled = lambda: True
        with self.assertRaises(AccessDenied): await self.api.page('a', 'fixture')
        self.assertEqual(self.client.calls, []); self.assertFalse(self.folder.exists())

    async def test_immediate_matches_main_and_archive_no_dialog_catalog_or_auxiliary_users(self):
        self.full(); result = await self.api.page('a', 'fixture')
        self.assertEqual([(r['chat_id'], r['id']) for r in result['messages']], [(11, 9), (22, 9)])
        self.assertTrue(result['complete']); self.assertEqual(result['phase'], 'done')
        self.assertEqual(result['catalog_chat_count'], 0); self.assertEqual(result['telegram_rpc_calls'], 2)
        self.assertNotIn('phone', json.dumps(result)); self.assertNotIn('access_hash', json.dumps(result))
        self.assertNotIn('users', result); self.assertNotIn('chats', result)
        self.assertEqual(type(self.client.calls[0]).__name__, 'SearchGlobalRequest')
        self.assertEqual(type(self.client.calls[1]).__name__, 'GetPeerDialogsRequest')

    async def test_exact_offset_triplet_short_slice_continues_peer_ids_do_not_collide(self):
        self.full()
        self.client.pages = [page([message(PEER_A, 9), message(PEER_B, 9)], sliced=True, rate=123),
                             page([message(PEER_B, 9), message(PEER_A, 8)])]
        first = await self.api.page('a', 'fixture', limit=50)
        self.assertTrue(first['partial']); self.assertIsNotNone(first['next_cursor'])
        second = await self.api.page('a', 'fixture', first['next_cursor'], limit=50)
        request = self.client.calls[2]
        self.assertEqual((request.offset_rate, request.offset_id), (123, 9))
        self.assertIsInstance(request.offset_peer, types.InputPeerUser)
        self.assertEqual(request.offset_peer.user_id, 22)
        self.assertEqual(request.max_date, self.client.calls[0].max_date)
        self.assertEqual([(r['chat_id'], r['id']) for r in second['messages']], [(11, 8)])
        self.assertTrue(second['complete'])
        before = len(self.client.calls)
        restarted = AccountSearchBackend(self.rt, self.registry, self.folder, converter)
        replay = await restarted.page('a', 'fixture', first['next_cursor'], limit=50)
        self.assertEqual(replay['messages'], second['messages']); self.assertTrue(replay['cached_replay'])
        self.assertEqual(replay['telegram_rpc_calls'], 0); self.assertEqual(len(self.client.calls), before)
        for p in self.folder.glob('*.json'): self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    async def test_missing_next_rate_uses_last_message_date_and_empty_page_finishes(self):
        self.full(); self.client.pages = [page([message(PEER_A, 9)], sliced=True), page([])]
        first = await self.api.page('a', 'fixture')
        second = await self.api.page('a', 'fixture', first['next_cursor'])
        self.assertEqual(self.client.calls[2].offset_rate, int(DATE.timestamp()))
        self.assertEqual(second['messages'], []); self.assertTrue(second['complete'])

    async def test_scope_query_limit_agent_expiry_and_post_rpc_revocation(self):
        self.full(); self.client.pages = [page([message(PEER_A, 9)], sliced=True, rate=123)]
        first = await self.api.page('a', 'fixture')
        for agent, query, size in [('a', 'changed', 50), ('a', 'fixture', 1), ('b', 'fixture', 50)]:
            with self.assertRaises(AccessDenied): await self.api.page(agent, query, first['next_cursor'], size)
        def narrow(_):
            self.data['agents']['a']['all_chats'] = False; self.registry.save(self.data)
        self.client.after_rpc = narrow
        before = len(self.client.calls)
        with self.assertRaises(AccessDenied): await self.api.page('a', 'new query')
        self.assertEqual(len(self.client.calls), before + 1)

    async def test_outside_dialog_left_channel_and_zero_history_never_return(self):
        self.full()
        channel = types.Channel(id=33, access_hash=3300, title='Unjoined synthetic',
            photo=types.ChatPhotoEmpty(), date=DATE, left=True, broadcast=True)
        peer_c = types.PeerChannel(33)
        self.client.pages = [page([message(PEER_A, 9), message(PEER_B, 8), message(peer_c, 7)],
            chats=[channel], dialogs=[dialog(PEER_A), dialog(PEER_B, top=0), dialog(peer_c)])]
        result = await self.api.page('a', 'fixture')
        self.assertEqual([r['chat_id'] for r in result['messages']], [11])
        self.assertEqual(result['omitted_records'], 2)
        self.assertNotIn('Unjoined', json.dumps(result))

    async def test_floodwait_retains_cursor_and_prevents_repeat_until_wait_elapsed(self):
        self.full(); self.client.flood = True
        first = await self.api.page('a', 'fixture')
        self.assertEqual(first['error'], 'telegram_cooldown'); self.assertIsNotNone(first['next_cursor'])
        before = len(self.client.calls)
        second = await self.api.page('a', 'fixture', first['next_cursor'])
        self.assertEqual(second['error'], 'telegram_cooldown'); self.assertEqual(len(self.client.calls), before)
        self.client.flood = False; self.api.retry_at = 0
        resumed = await self.api.page('a', 'fixture', second['next_cursor'])
        self.assertEqual(len(resumed['messages']), 2)

    async def test_byte_limit_never_advances_a_guessed_rate_or_skips_rows(self):
        self.full(); self.client.pages = [page([message(PEER_A, 9, 'x' * 1200001)], sliced=True, rate=44)]
        result = await self.api.page('a', 'fixture')
        self.assertEqual(result['error'], 'account_search_page_exceeds_byte_limit')
        stored = self.api.cursors.load(result['next_cursor'], self.api.binding(self.registry.profile('a'), 'fixture', 50))
        self.assertEqual(stored['offset_id'], 0); self.assertEqual(stored['offset_rate'], 0)

    async def test_same_server_page_does_not_create_endless_cursor_and_inexact_is_not_complete(self):
        self.full(); self.client.pages = [page([message(PEER_A, 9)], sliced=True, rate=44)]
        first = await self.api.page('a', 'fixture')
        second = await self.api.page('a', 'fixture', first['next_cursor'])
        self.assertEqual(second['error'], 'account_search_page_failed')
        self.client.pages = [page([], inexact=False)]; self.client.index = 0
        done = await self.api.page('a', 'other')
        self.assertTrue(done['complete'])


class FastFacadeTests(RegistryFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_registry(); self.data['agents']['a']['all_chats'] = True; self.registry.save(self.data)
        self.client = FakeMTProto()
        self.backend = AccountSearchBackend(fake_runtime(self.client), self.registry,
                                            Path(self.temporary.name) / 'backend', converter)
        self.calls = []
        owner = self
        class Bridge:
            async def call(self, name, args):
                owner.calls.append((name, args))
                assert name == 'read_account_search_page'
                return await owner.backend.page(args['agent_id'], args['query'], args.get('cursor'), args['limit'])
        self.api = ReaderGateway(self.registry, Bridge(), search_cursor_dir=Path(self.temporary.name) / 'frontend')

    def tearDown(self): self.teardown_registry()

    async def test_full_scope_returns_matches_in_first_budget_one_call(self):
        result = await self.api.search_allowed('a', 'fixture', request_budget=1)
        self.assertEqual(len(result['messages']), 2); self.assertEqual(result['catalog_chat_count'], 0)
        self.assertEqual(len(self.calls), 1); self.assertEqual(result['backend_calls'], 1)
        self.assertNotIn('verified_chat_ids', result)

    async def test_catalog_only_cursor_migrates_without_discarding_any_delivered_matches(self):
        binding = self.api.search_cursors.binding(self.registry.profile('a'), 'fixture', 50)
        token = self.api.search_cursors.save({'binding': binding, 'expires_at': 9999999999,
            'phase': 'catalog', 'index': 0, 'chats': [{'id': i} for i in range(1000)]})
        result = await self.api.search_allowed('a', 'fixture', token)
        self.assertEqual(result['cursor_migrated_from'], 'catalog_only_v1')
        self.assertEqual(len(result['messages']), 2); self.assertEqual(len(self.calls), 1)

    async def test_restricted_cannot_request_account_backend_or_forge_account_cursor(self):
        with self.assertRaises(PermissionError):
            await self.api.call('b', 'search', 'read_account_search_page', {'agent_id': 'a', 'query': 'fixture'})
        with self.assertRaises(ValueError): await self.api.search_allowed('b', 'fixture', 'a2:' + 'z' * 32)
        self.assertEqual(self.calls, [])
