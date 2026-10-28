"""Structured search on the existing account; no join, read receipts or media."""
import re
import asyncio
from typing import Optional

from .history_pages import bounded_records


def validate_query(query):
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise ValueError('invalid_search_query')


def register_search_pages(runtime):
    from telegram_mcp.tools.messages import message_to_dict
    from telethon.tl import functions, types
    from .account_search import register_account_search
    register_account_search(runtime)

    @runtime.mcp.tool(annotations=runtime.ToolAnnotations(
        title='Read one chat search page', readOnlyHint=True, openWorldHint=True))
    @runtime.with_account(readonly=True)
    async def read_search_page(chat_id: int, query: str, before_id: int = 0,
                               until_id: Optional[int] = None, limit: int = 50,
                               account: Optional[str] = None) -> dict:
        """Descending search with an inclusive per-chat snapshot and exclusive cursor."""
        validate_query(query)
        if (type(chat_id) is not int or chat_id == 0 or type(limit) is not int
                or not 1 <= limit <= 100 or type(before_id) is not int or before_id < 0
                or (until_id is not None and (type(until_id) is not int or until_id < 0))):
            raise ValueError('invalid_search_page')
        client = runtime.get_client(account)
        entity = await runtime.resolve_entity(chat_id, client)
        if runtime.get_marked_id(entity) != chat_id:
            raise ValueError('unexpected_chat_identity')
        if runtime.is_chat_allowlist_enabled() and not runtime.is_chat_allowed(chat_id, entity):
            raise ValueError('chat_not_permitted')
        if until_id is None:
            latest = await client.get_messages(entity, limit=1)
            until_id = latest[0].id if latest else 0
        if not until_id:
            return {'chat_id': chat_id, 'messages': [], 'until_id': 0,
                    'next_before_id': None, 'has_more': False}
        boundary = min(until_id + 1, before_id) if before_id else until_id + 1
        messages = await client.get_messages(entity, search=query, limit=limit + 1, max_id=boundary)
        rows = [message_to_dict(m, chat_id, bool(getattr(entity, 'forum', False)))
                for m in messages if 0 < m.id < boundary]
        selected, size = bounded_records(rows, limit=limit)
        more = len(rows) > len(selected)
        return {'chat_id': chat_id, 'messages': selected, 'until_id': until_id,
                'next_before_id': selected[-1]['id'] if more else None,
                'has_more': more, 'json_bytes': size, 'untrusted_content': True}

    @runtime.mcp.tool(annotations=runtime.ToolAnnotations(
        title='Search public community metadata', readOnlyHint=True, openWorldHint=True))
    @runtime.with_account(readonly=True)
    async def search_community_metadata(query: str, limit: int = 20,
                                        account: Optional[str] = None) -> dict:
        """Only public groups/channels. Never return users, bots, contacts or history."""
        validate_query(query)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid_page_size')
        async def read_metadata():
            client = runtime.get_client(account)
            await runtime.ensure_connected(client)
            reply = await client(functions.contacts.SearchRequest(q=query, limit=limit), flood_sleep_threshold=0)
            output = []
            # Do not inspect reply.users: Telegram may include people in this RPC.
            for entity in reply.chats:
                username = getattr(entity, 'username', None)
                if (not isinstance(entity, types.Channel) or not isinstance(username, str)
                        or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', username)
                        or getattr(entity, 'min', False) or getattr(entity, 'deactivated', False)):
                    continue
                numeric_id = runtime.get_marked_id(entity)
                if runtime.is_chat_allowlist_enabled() and not runtime.is_chat_allowed(numeric_id, entity):
                    continue
                output.append({'id': numeric_id, 'title': runtime.sanitize_name(entity.title),
                               'username': username, 'type': 'group' if entity.megagroup else 'channel',
                               'url': 'https://t.me/' + username})
            return {'communities': output[:limit], 'pagination_supported': False,
                    'complete': False, 'coverage': 'telegram_ranked_metadata_matches',
                    'joined': False, 'history_read': False, 'untrusted_content': True}
        try:
            return await asyncio.wait_for(read_metadata(), timeout=25)
        except Exception as exc:
            # Only type/category, never exception text, query, entities or credentials.
            from telethon.errors import FloodWaitError, RPCError
            kind = type(exc).__name__
            seconds = getattr(exc, 'seconds', None)
            if isinstance(exc, FloodWaitError) or (isinstance(exc,RPCError) and getattr(exc,'code',None)==420 and type(seconds) is int):
                return {'error': 'telegram_cooldown', 'leaf_error_category': 'flood_wait',
                        'leaf_error_type': kind if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}',kind) else 'FloodWaitError',
                        **({'retry_after_seconds': max(1, seconds)} if type(seconds) is int else {})}
            if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
                category = 'transport'
            elif isinstance(exc, RPCError):
                category = 'telegram_rpc'
            else:
                category = 'backend_exception'
            return {'error': 'public_search_failed', 'leaf_error_category': category,
                    'leaf_error_type': kind if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}', kind) else 'Exception'}
