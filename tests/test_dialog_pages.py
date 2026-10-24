import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from telegram_setup.dialog_pages import decode_cursor, encode_cursor, register_dialog_page


def dialog(number):
    return SimpleNamespace(entity=SimpleNamespace(id=number, title='Fixture ' + str(number)),
                           message=SimpleNamespace(id=number, date=datetime(2026, 1, 1, tzinfo=timezone.utc)))


class DialogTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, client, allowed=None):
        functions = {}
        def tool(**_kwargs):
            def register(fn): functions[fn.__name__] = fn; return fn
            return register
        runtime = SimpleNamespace(mcp=SimpleNamespace(tool=tool),
            ToolAnnotations=lambda **kwargs: kwargs, with_account=lambda **kwargs: lambda fn: fn,
            get_client=lambda account: client, get_marked_id=lambda entity: entity.id,
            is_chat_allowlist_enabled=lambda: allowed is not None,
            is_chat_allowed=lambda number, entity: number in allowed,
            person_name=lambda entity: 'Fixture', sanitize_name=lambda text: text)
        register_dialog_page(runtime)
        return functions['read_dialog_page']

    async def test_bounded_metadata_and_cursor(self):
        client = SimpleNamespace(get_dialogs=AsyncMock(return_value=[dialog(1), dialog(2), dialog(3)]),
                                 get_input_entity=AsyncMock(return_value='cached-peer'))
        read = self.runtime(client)
        first = await read(limit=2)
        self.assertEqual([x['id'] for x in first['chats']], [1, 2])
        self.assertTrue(first['has_more'])
        self.assertEqual(client.get_dialogs.call_args.kwargs, {'limit': 3, 'archived': False})
        client.get_dialogs.return_value = [dialog(3)]
        second = await read(cursor=first['next_cursor'], limit=2)
        self.assertEqual([x['id'] for x in second['chats']], [3])
        self.assertFalse(second['has_more'])
        self.assertEqual(client.get_dialogs.call_args.kwargs['offset_id'], 2)
        client.get_input_entity.assert_awaited_once_with(2)

    async def test_filtered_empty_page_keeps_cursor(self):
        client = SimpleNamespace(get_dialogs=AsyncMock(return_value=[dialog(1), dialog(2)]))
        result = await self.runtime(client, allowed={99})(limit=1)
        self.assertEqual(result['chats'], [])
        self.assertTrue(result['next_cursor'])

    def test_invalid_cursor_and_archive_change(self):
        token = encode_cursor(dialog(1), 1, False)
        self.assertEqual(decode_cursor(token, False)[0]['peer'], 1)
        for cursor in ['invalid', 'x' * 3000, None]:
            with self.assertRaises(ValueError): decode_cursor(cursor, False)
        with self.assertRaises(ValueError): decode_cursor(token, True)


if __name__ == '__main__': unittest.main()
