"""Fast full-account search; mandatory permitted-ID fan-out for restricted agents."""
import copy
import hashlib
import json
from pathlib import Path
import re
import time

from .agents import AccessDenied
from .private_state import opaque_id, read_private, secure_directory, write_json
from .search_pages import validate_query


class SearchCursors:
    def __init__(self, folder, ttl=3600):
        self.folder, self.ttl = Path(folder), ttl

    @staticmethod
    def binding(profile, query, limit):
        scope = {key: profile.get(key) for key in ('agent', 'all_chats', 'chat_ids', 'actions', 'expires_at')}
        return hashlib.sha256(json.dumps([scope, query, limit], sort_keys=True).encode()).hexdigest()

    def load(self, token, binding):
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32}', token):
            raise ValueError('invalid_search_cursor')
        try:
            secure_directory(self.folder)
            state = json.loads(read_private(self.folder / (token + '.json')))
        except FileNotFoundError:
            raise ValueError('search_cursor_missing_or_expired') from None
        if state.get('binding') != binding or state.get('expires_at', 0) <= time.time():
            raise AccessDenied('search_cursor_scope_changed_or_expired')
        return state

    def save(self, state, token=None):
        secure_directory(self.folder)
        # Expired cursor data is removed opportunistically, never source chats.
        for path in self.folder.glob('*.json'):
            try:
                if json.loads(read_private(path)).get('expires_at', 0) <= time.time():
                    path.unlink()
            except FileNotFoundError:
                pass
        if len(list(self.folder.glob('*.json'))) >= 4096 and token is None:
            raise ValueError('search_cursor_capacity_reached')
        token = token or opaque_id()
        write_json(self.folder / (token + '.json'), state)
        return token


class ScopedSearchMixin:
    async def search_allowed(self, agent, query, cursor=None, limit=50, request_budget=10):
        """Each page uses at most request_budget backend calls; cursor retains remaining work."""
        from .reader_gateway import bounded_int
        validate_query(query); bounded_int(limit); bounded_int(request_budget, 20)
        profile = self.policy(agent, 'search')
        if profile.get('all_chats', False):
            return await self.search_full_account(agent, query, cursor, limit)
        async with self.search_lock:
            profile = self.policy(agent, 'search')
            if profile.get('all_chats', False):
                raise AccessDenied('search_scope_changed_retry_query')
            binding = self.search_cursors.binding(profile, query, limit)
            if cursor:
                state = self.search_cursors.load(cursor, binding)
                if state.get('response') is not None:
                    # Scope/expiry were rechecked before returning cached private messages.
                    return state['response']
                state = copy.deepcopy(state)
            else:
                state = {'binding': binding, 'expires_at': int(time.time()) + self.search_cursors.ttl,
                         'phase': 'search',
                         'archived': False, 'dialog_cursor': None, 'chats': [], 'index': 0,
                         'before_id': 0, 'until_id': None, 'errors': [], 'snapshot_started_at': int(time.time())}
                state['chats'] = [{'id': i} for i in sorted(set(profile.get('chat_ids', [])))]
            output, calls, retry = [], 0, None
            deadline = time.monotonic() + 45
            while calls < request_budget and len(output) < limit and time.monotonic() < deadline:
                current = self.policy(agent, 'search')
                if self.search_cursors.binding(current, query, limit) != binding:
                    raise AccessDenied('search_cursor_scope_changed_or_expired')
                if state['phase'] not in ('search', 'done'):
                    raise ValueError('invalid_restricted_search_cursor_phase')
                if state['index'] >= len(state['chats']):
                    state['phase'] = 'done'; break
                chat = state['chats'][state['index']]
                ident = chat['id']
                self.policy(agent, 'search', ident)
                args = {'chat_id': ident, 'query': query, 'before_id': state['before_id'],
                        'until_id': state['until_id'], 'limit': limit - len(output)}
                result = await self.call(agent, 'search', 'read_search_page', args, ident)
                calls += 1
                if isinstance(result, dict) and result.get('error'):
                    if result.get('error') == 'telegram_cooldown':
                        retry = result; break
                    state['errors'].append({'chat_id': ident, 'error': 'search_failed'})
                    state.update(index=state['index'] + 1, before_id=0, until_id=None)
                    continue
                self.policy(agent, 'search', ident)  # Revoke or narrow while awaiting RPC.
                if (not isinstance(result, dict) or result.get('chat_id') != ident
                        or not isinstance(result.get('messages'), list)
                        or type(result.get('until_id')) is not int):
                    raise RuntimeError('unexpected_search_identity_or_shape')
                if state['until_id'] is not None and result['until_id'] != state['until_id']:
                    raise RuntimeError('search_snapshot_changed')
                rows = result['messages']
                previous = state['before_id'] or result['until_id'] + 1
                if len(rows) > args['limit']:
                    raise RuntimeError('oversized_search_page')
                selected = []
                current_bytes = len(json.dumps(output, ensure_ascii=False).encode())
                for row in rows:
                    if (not isinstance(row, dict) or type(row.get('id')) is not int
                            or not 0 < row['id'] < previous
                            or row.get('chat_id', ident) != ident or row['id'] > result['until_id']):
                        raise RuntimeError('unexpected_search_message_identity')
                    record = dict(row, chat_id=ident)
                    cost = len(json.dumps(record, ensure_ascii=False).encode())
                    if current_bytes + cost > 1500000:
                        if not output and not selected:
                            raise ValueError('single_search_message_exceeds_output_limit')
                        break
                    selected.append(record)
                    current_bytes += cost
                    previous = row['id']
                output.extend(selected)
                trimmed = len(selected) < len(rows)
                if result.get('has_more') or trimmed:
                    if not rows or result.get('next_before_id') != rows[-1]['id']:
                        if not trimmed:
                            raise RuntimeError('non_progressing_search_cursor')
                    if selected:
                        state.update(before_id=selected[-1]['id'], until_id=result['until_id'])
                else:
                    state.update(index=state['index'] + 1, before_id=0, until_id=None)
                if trimmed:
                    break
            current = self.policy(agent, 'search')
            if self.search_cursors.binding(current, query, limit) != binding:
                raise AccessDenied('search_cursor_scope_changed_or_expired')
            done = state['phase'] == 'done' or (state['phase'] == 'search' and state['index'] >= len(state['chats']))
            # The cursor remains at the exact failed operation on FloodWait/catalog errors.
            more = not done
            next_cursor = self.search_cursors.save(state) if more else None
            response = {'messages': output, 'next_cursor': next_cursor, 'complete': done and not state['errors'],
                        'scan_finished': done, 'partial': more or bool(state['errors']),
                        'phase': state['phase'], 'catalog_chat_count': len(state['chats']),
                        'chats_completed': state['index'], 'backend_calls': calls,
                        'errors': state['errors'], 'snapshot_started_at': state['snapshot_started_at'],
                        'snapshot_kind': 'catalog_then_per_chat_id_boundaries', 'untrusted_content': True}
            if retry:
                response.update(retry)
            if cursor:
                # A lost response may be replayed without downloading/searching again.
                source = self.search_cursors.load(cursor, binding)
                # Cooldown/error responses must remain resumable after the wait.
                if not retry:
                    source['response'] = response
                    self.search_cursors.save(source, cursor)
            return response

    async def search_full_account(self, agent, query, cursor, limit):
        """One bounded backend page, no catalog warm-up or per-chat prefix re-fetch."""
        async with self.search_lock:
            profile = self.policy(agent, 'search')
            if not profile.get('all_chats', False):
                raise AccessDenied('account_search_requires_all_chats')
            binding = self.search_cursors.binding(profile, query, limit)
            migrated = False
            if cursor is not None:
                if isinstance(cursor, str) and re.fullmatch(r'a2:[A-Za-z0-9_-]{32}', cursor):
                    backend_cursor = cursor[3:]
                else:
                    old = self.search_cursors.load(cursor, binding)
                    # Catalog-only continuations have delivered no search results yet.
                    if old.get('phase') != 'catalog' or old.get('index', 0) != 0:
                        raise ValueError('search_engine_changed_restart_query_without_cursor')
                    backend_cursor, migrated = None, True
            else:
                backend_cursor = None
            result = await self.call(agent, 'search', 'read_account_search_page',
                {'agent_id': agent, 'query': query, 'cursor': backend_cursor, 'limit': limit})
            current = self.policy(agent, 'search')
            if not current.get('all_chats') or self.search_cursors.binding(current, query, limit) != binding:
                raise AccessDenied('search_cursor_scope_changed_or_expired')
            if not isinstance(result, dict):
                raise RuntimeError('unexpected_account_search_response')
            token = result.get('next_cursor')
            if token is not None and (not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32}', token)):
                raise RuntimeError('invalid_account_search_backend_cursor')
            response = dict(result, next_cursor='a2:' + token if token else None, backend_calls=1)
            if result.get('error'):
                # A local cooldown may stop the call before the backend sees its cursor.
                if not token and backend_cursor:
                    response['next_cursor'] = 'a2:' + backend_cursor
                return response
            if result.get('search_engine') != 'account_global_v2' or not isinstance(result.get('messages'), list):
                raise RuntimeError('unexpected_account_search_engine')
            verified = result.get('verified_chat_ids')
            if not isinstance(verified, list) or any(type(i) is not int or i == 0 for i in verified):
                raise RuntimeError('account_search_verified_peers_missing')
            if len(result['messages']) > limit:
                raise RuntimeError('oversized_search_page')
            seen = set()
            for row in result['messages']:
                if (not isinstance(row, dict) or type(row.get('chat_id')) is not int
                        or row['chat_id'] not in verified or type(row.get('id')) is not int or row['id'] <= 0):
                    raise RuntimeError('unexpected_search_message_identity')
                self.policy(agent, 'search', row['chat_id'])
                pair = (row['chat_id'], row['id'])
                if pair in seen:
                    raise RuntimeError('duplicate_account_search_message')
                seen.add(pair)
            # No internal routing data, raw users/chats, or backend extras reach the client.
            allowed = {'messages', 'next_cursor', 'complete', 'scan_finished', 'partial', 'phase',
                'search_engine', 'catalog_chat_count', 'chats_completed', 'messages_examined',
                'messages_examined_total', 'omitted_records', 'telegram_rpc_calls',
                'snapshot_started_at', 'snapshot_kind', 'coverage', 'inexact', 'untrusted_content', 'backend_calls', 'cached_replay'}
            response = {k: v for k, v in response.items() if k in allowed}
            if migrated:
                response['cursor_migrated_from'] = 'catalog_only_v1'
            return response

    async def public_communities(self, agent, query, limit=20):
        from .reader_gateway import bounded_int
        validate_query(query); bounded_int(limit)
        self.policy(agent, 'public-search')
        result = await self.call(agent, 'public-search', 'search_community_metadata',
                                 {'query': query, 'limit': limit})
        self.policy(agent, 'public-search')
        if isinstance(result, dict) and result.get('error'):
            return result
        if not isinstance(result, dict) or not isinstance(result.get('communities'), list):
            raise RuntimeError('unexpected_public_search_shape')
        output = []
        for row in result['communities']:
            if (not isinstance(row, dict) or row.get('type') not in ('group', 'channel')
                    or type(row.get('id')) is not int or row['id'] >= 0
                    or not isinstance(row.get('username'), str)
                    or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', row['username'])):
                continue
            output.append({key: row[key] for key in ('id', 'title', 'username', 'type') if key in row})
            output[-1]['url'] = 'https://t.me/' + row['username']
        return {'communities': output[:limit], 'pagination_supported': False, 'complete': False,
                'coverage': 'telegram_ranked_metadata_matches', 'joined': False,
                'history_read': False, 'untrusted_content': True}
