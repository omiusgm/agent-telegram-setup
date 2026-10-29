"""Small read-only facade; no arbitrary upstream tool forwarding."""
import asyncio
import re
import time

from .agents import check_action
from .scoped_search import ScopedSearchMixin, SearchCursors
from .sensitive_peer import deny_sensitive_peer, is_sensitive_peer

UPSTREAM_READS = {
    'status': {'get_me'}, 'dialogs': {'read_dialog_page', 'get_chat'},
    'contacts': {'list_contacts'}, 'history': {'read_history_page'},
    'search': {'search_messages', 'read_search_page', 'read_account_search_page'}, 'messages': {'get_messages'},
    'public-search': {'search_community_metadata'},
}


def records(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get('results'), list):
        return payload['results']
    if isinstance(payload, str) and payload in ('No contacts found.', 'Page out of range.'):
        return []
    raise RuntimeError('unexpected_upstream_record_shape')


def bounded_int(value, maximum=100):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError('invalid_page_size')
    return value


class ReaderGateway(ScopedSearchMixin):
    def __init__(self, registry, upstream, *, search_cursor_dir=None):
        from .config import private_dir
        self.registry, self.upstream = registry, upstream
        self.lock = asyncio.Lock()
        self.search_lock = asyncio.Lock()
        self.search_cursors = SearchCursors(search_cursor_dir or private_dir() / 'search-cursors')
        self.retry_at = 0.0

    def policy(self, agent, action, chat_id=None):
        profile = self.registry.profile(agent)
        if chat_id is not None:
            deny_sensitive_peer(chat_id)
        check_action(profile, action, chat_id)
        return profile

    async def call(self, agent, action, name, args, chat_id=None):
        if name not in UPSTREAM_READS.get(action, set()):
            raise PermissionError('upstream_tool_not_permitted')
        async with self.lock:
            # Re-read under the shared call lock: queued work must honour revocation.
            profile = self.policy(agent, action, chat_id)
            if name == 'read_account_search_page' and (not profile.get('all_chats', False)
                    or args.get('agent_id') != agent):
                raise PermissionError('account_search_requires_own_full_scope')
            remaining = self.retry_at - time.monotonic()
            if remaining > 0:
                return {'error': 'telegram_cooldown', 'retry_after_seconds': int(remaining) + 1}
            result = await self.upstream.call(name, args)
            if isinstance(result, dict) and result.get('error'):
                diagnostic = {}
                if name == 'search_community_metadata':
                    if result.get('leaf_error_category') in ('flood_wait','transport','telegram_rpc','backend_exception','capability_missing'):
                        diagnostic['leaf_error_category'] = result['leaf_error_category']
                    kind = result.get('leaf_error_type')
                    if isinstance(kind,str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}',kind):
                        diagnostic['leaf_error_type'] = kind
                wait = result.get('retry_after_seconds', result.get('retry_after'))
                if type(wait) in (int, float) and wait > 0:
                    self.retry_at = time.monotonic() + wait
                    output = {'error': 'telegram_cooldown', 'retry_after_seconds': int(wait) + 1, **diagnostic}
                    if name == 'read_account_search_page' and isinstance(result.get('next_cursor'), str) and re.fullmatch(r'[A-Za-z0-9_-]{32}', result['next_cursor']):
                        output['next_cursor'] = result['next_cursor']
                    return output
                if name == 'search_community_metadata' and result.get('error') in ('public_search_failed','public_search_backend_unavailable','telegram_cooldown'):
                    return {'error':result['error'],**diagnostic}
                if name == 'read_account_search_page' and result.get('error') in (
                        'account_search_page_failed', 'account_search_page_exceeds_byte_limit'):
                    return {k: result[k] for k in ('error', 'next_cursor') if k in result}
                return {'error': 'upstream_read_failed'}
            return result

    async def status(self, agent):
        profile = self.policy(agent, 'status')
        result = await self.call(agent, 'status', 'get_me', {})
        connected = isinstance(result, dict) and result.get('type') == 'user' and not result.get('error')
        output = {'agent': agent, 'connected': connected, 'read_only': True,
                  'all_chats': profile.get('all_chats', False),
                  'sensitive_botfather_excluded': True,
                  'allowed_chat_count': len(profile.get('chat_ids', []))}
        if not connected:
            # Failed identity observation is not proof of revoked authorization.
            # Only fixed diagnostic codes survive; never relay raw get_me data.
            error = result.get('error') if isinstance(result, dict) else None
            output['connection_error'] = (error if error in ('telegram_cooldown', 'upstream_read_failed')
                                          else 'unexpected_status_payload')
            wait = result.get('retry_after_seconds') if isinstance(result, dict) else None
            if error == 'telegram_cooldown' and type(wait) is int and wait > 0:
                output['retry_after_seconds'] = wait
        return output

    async def dialogs(self, agent, cursor=None, limit=20, archived=False):
        profile = self.policy(agent, 'dialogs')
        bounded_int(limit, 50)
        if type(archived) is not bool:
            raise ValueError('invalid_archived_filter')
        if profile.get('all_chats', False):
            if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 2048):
                raise ValueError('invalid_dialog_cursor')
            return await self.call(agent, 'dialogs', 'read_dialog_page',
                                   {'cursor': cursor, 'limit': limit, 'archived': archived})
        if cursor is not None and (not isinstance(cursor, str) or not re.fullmatch(r'r:[0-9]{1,5}', cursor)):
            raise ValueError('invalid_restricted_dialog_cursor')
        offset = int(cursor[2:]) if cursor else 0
        ids = sorted(i for i in set(profile.get('chat_ids', [])) if not is_sensitive_peer(i))
        chosen = ids[offset:offset+limit]
        output = []
        for chat_id in chosen:
            result = await self.call(agent, 'dialogs', 'get_chat', {'chat_id': chat_id}, chat_id)
            if isinstance(result, dict) and result.get('error'):
                return result
            if not isinstance(result, dict) or result.get('id') != chat_id:
                raise RuntimeError('unexpected_chat_identity')
            output.append({key: result[key] for key in ('id', 'title', 'name', 'type', 'username') if key in result})
        next_cursor = 'r:' + str(offset+limit) if offset+limit < len(ids) else None
        return {'chats': output, 'next_cursor': next_cursor, 'has_more': bool(next_cursor),
                'untrusted_content': True, 'archived_filter_supported': False}

    async def contacts(self, agent, page=1, limit=50, query=None):
        profile = self.policy(agent, 'contacts')
        bounded_int(page, 10000); bounded_int(limit, 100)
        if query is not None and (not isinstance(query, str) or not query.strip() or len(query) > 500):
            raise ValueError('invalid_contact_query')
        ids = set(profile.get('contact_ids', []))
        if not profile.get('all_contacts', False) and not ids:
            return {'contacts': [], 'next_page': None, 'untrusted_content': True}
        result = await self.call(agent, 'contacts', 'list_contacts', {})
        if isinstance(result, dict) and result.get('error'):
            return result
        selected = [r for r in records(result) if profile.get('all_contacts', False) or r.get('id') in ids]
        if query:
            selected = [r for r in selected if query.casefold() in
                        (str(r.get('name', '')) + ' ' + str(r.get('username', ''))).casefold()]
        selected.sort(key=lambda r: r['id'])
        fields = ('id', 'name', 'username', 'phone') if profile.get('include_phones', False) else ('id', 'name', 'username')
        output = [{key: r[key] for key in fields if key in r} for r in selected[(page-1)*limit:page*limit]]
        return {'contacts': output, 'next_page': page + 1 if page*limit < len(selected) else None,
                'untrusted_content': True}

    async def history(self, agent, chat_id, after_id=0, until_id=None, limit=100):
        self.policy(agent, 'history', chat_id)
        bounded_int(limit)
        if type(after_id) is not int or after_id < 0 or (until_id is not None and (type(until_id) is not int or until_id < 0)):
            raise ValueError('invalid_history_boundary')
        return await self.call(agent, 'history', 'read_history_page',
            {'chat_id': chat_id, 'after_id': after_id, 'until_id': until_id, 'limit': limit}, chat_id)

    async def search(self, agent, chat_id, query, limit=50):
        self.policy(agent, 'search', chat_id); bounded_int(limit)
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ValueError('invalid_search_query')
        result = await self.call(agent, 'search', 'search_messages',
                                 {'chat_id': chat_id, 'query': query, 'limit': limit}, chat_id)
        return {'result': result, 'untrusted_content': True}

    async def messages(self, agent, chat_id, page=1, limit=20):
        self.policy(agent, 'messages', chat_id)
        bounded_int(page, 10000); bounded_int(limit)
        result = await self.call(agent, 'messages', 'get_messages',
                                 {'chat_id': chat_id, 'page': page, 'page_size': limit}, chat_id)
        return {'result': result, 'untrusted_content': True}
