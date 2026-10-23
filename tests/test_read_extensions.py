import asyncio
import json
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from telethon.errors import FloodWaitError
from telethon.tl.types import Channel, Chat, ChatPhotoEmpty, User

from telegram_setup import history_pages, owned_inventory


class Registry:
    def __init__(self):
        self.tools = {}

    def tool(self, **kwargs):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


def runtime(client):
    from mcp.types import ToolAnnotations
    return NS(mcp=Registry(), ToolAnnotations=ToolAnnotations,
        with_account=lambda **_: lambda fn: fn,
        validate_id=lambda *_: lambda fn: fn,
        get_client=lambda _: client,
        ensure_connected=AsyncMock(), resolve_entity=AsyncMock(),
        is_chat_allowlist_enabled=lambda: False, is_chat_allowed=lambda *_: True,
        get_marked_id=lambda _: -100001)


def group(number, *, creator=True, **kwargs):
    return Channel(id=number, title='Synthetic group', photo=ChatPhotoEmpty(),
        date=datetime.now(timezone.utc), creator=creator, megagroup=True, **kwargs)


class Cursor:
    def __init__(self, items):
        self.items = iter(items)

    def __aiter__(self):
        raise AssertionError('Resuming a page must not reset the iterator')

    async def __anext__(self):
        try:
            value = next(self.items)
        except StopIteration:
            raise StopAsyncIteration
        if isinstance(value, Exception):
            raise value
        return value


class InventoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        owned_inventory._INVENTORIES.clear()
        owned_inventory._INVENTORY_LOCKS.clear()

    def make_tool(self, dialogs, bots=()):
        client = NS(iter_dialogs=lambda **_: Cursor(dialogs))
        # Special methods are resolved on the type, not instance attributes.
        class Client:
            def iter_dialogs(self, **kwargs):
                self.options = kwargs
                return Cursor(dialogs)

            async def __call__(self, _):
                return list(bots)
        client = Client()
        rt = runtime(client)
        owned_inventory.register_owned_inventory(rt)
        return rt, client, rt.mcp.tools['read_owned_inventory']

    async def test_owner_not_administrator_and_archived_included(self):
        rt, client, tool = self.make_tool([
            NS(entity=group(10), archived=True),
            NS(entity=group(11, creator=False), archived=False),
            NS(entity=group(12, left=True), archived=False)])
        result = await tool()
        self.assertTrue(result['complete'])
        self.assertEqual(client.options['archived'], None)
        self.assertEqual([c['chat_id'] for c in result['chats']], [-1000000000010])
        self.assertTrue(result['chats'][0]['archived'])
        self.assertFalse(result['historical_chats'][0]['folder_eligible'])

    async def test_resume_keeps_cursor_and_does_not_duplicate(self):
        _, _, tool = self.make_tool([NS(entity=group(i), archived=False) for i in range(1, 202)])
        first = await tool()
        self.assertFalse(first['complete'])
        self.assertEqual(first['counts']['dialogs_scanned'], 200)
        second = await tool()
        self.assertTrue(second['complete'])
        self.assertEqual(second['counts']['dialogs_scanned'], 201)
        self.assertEqual(len({c['chat_id'] for c in second['chats']}), 201)

    async def test_floodwait_never_bypassed_by_next_call(self):
        _, _, tool = self.make_tool([NS(entity=group(1), archived=False),
                                    FloodWaitError(request=None, capture=12),
                                    NS(entity=group(2), archived=False)])
        with patch.object(owned_inventory.time, 'monotonic', return_value=100):
            first = await tool()
            repeated = await tool()
        self.assertFalse(first['complete'])
        self.assertEqual(repeated['counts']['dialogs_scanned'], 1)
        self.assertEqual(repeated['retry_after_seconds'], 12)
        with patch.object(owned_inventory.time, 'monotonic', return_value=113):
            resumed = await tool()
        self.assertTrue(resumed['complete'])
        self.assertEqual(resumed['counts']['dialogs_scanned'], 2)

    async def test_bots_without_dialog_and_allowlist(self):
        bot = User(id=40, bot=True, first_name='Synthetic bot', username='synthetic_bot')
        rt, _, tool = self.make_tool([NS(entity=group(10), archived=False)], [bot])
        rt.is_chat_allowlist_enabled = lambda: True
        rt.is_chat_allowed = lambda peer, _: peer == 40
        result = await tool()
        self.assertEqual(result['chats'], [])
        self.assertEqual(result['counts']['allowlist_filtered'], 1)
        self.assertEqual(result['bots'][0]['chat_id'], 40)
        self.assertFalse(result['bots'][0]['has_dialog'])
        self.assertNotIn('access_hash', json.dumps(result))

    async def test_partial_scan_can_be_cancelled_without_more_reads(self):
        rt, client, tool = self.make_tool([NS(entity=group(i), archived=False) for i in range(1, 202)])
        await tool()
        result = await tool(cancel=True)
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['telegram_writes'], 0)
        self.assertFalse(owned_inventory.has_pending_inventory())


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_boundaries_and_cursor(self):
        client = NS(get_messages=AsyncMock(return_value=[NS(id=i) for i in (4, 6, 8)]))
        rt = runtime(client)
        rt.resolve_entity.return_value = NS(forum=True)
        converter = NS(message_to_dict=lambda m, *_: {'id': m.id})
        with patch.dict(sys.modules, {'telegram_mcp.tools.messages': converter}):
            history_pages.register_history_page(rt)
            result = await rt.mcp.tools['read_history_page'](123, after_id=2, until_id=8, limit=2)
        self.assertEqual(client.get_messages.await_args.kwargs,
                         {'limit': 3, 'min_id': 2, 'max_id': 9, 'reverse': True})
        self.assertEqual(result['next_after_id'], 6)
        self.assertTrue(result['has_more'])

    async def test_disallowed_chat_never_fetches_history(self):
        client = NS(get_messages=AsyncMock())
        rt = runtime(client)
        rt.is_chat_allowlist_enabled = lambda: True
        rt.is_chat_allowed = lambda *_: False
        with patch.dict(sys.modules, {'telegram_mcp.tools.messages': NS(message_to_dict=lambda *_: {})}):
            history_pages.register_history_page(rt)
        with self.assertRaises(ValueError):
            await rt.mcp.tools['read_history_page'](123)
        client.get_messages.assert_not_awaited()

    def test_byte_limit_does_not_skip_a_message(self):
        rows = [{'id': 1, 'text': 'а' * 10}, {'id': 2, 'text': 'б' * 10}]
        cost = len(json.dumps(rows[0], ensure_ascii=False).encode())
        page, size = history_pages.bounded_records(rows, byte_limit=cost)
        self.assertEqual([r['id'] for r in page], [1])
        self.assertEqual(size, cost)
        with self.assertRaises(ValueError):
            history_pages.bounded_records(rows, byte_limit=1)


if __name__ == '__main__':
    unittest.main()
