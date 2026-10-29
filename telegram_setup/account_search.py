"""Bounded account search on the one existing client, with server-owned cursors."""
import asyncio
import copy
from datetime import datetime, timezone
import json
import time
from typing import Optional

from telethon import utils
from telethon.tl import functions, types

from .agents import AccessDenied, AgentRegistry, check_action
from .scoped_search import SearchCursors
from .search_pages import validate_query
from .sensitive_peer import is_sensitive_peer, redact_tokens

ENGINE = 'account_global_v2'
PAGE_BYTES = 1200000


def input_peer_record(peer):
    allowed = (types.InputPeerEmpty, types.InputPeerSelf, types.InputPeerUser,
               types.InputPeerChat, types.InputPeerChannel)
    if not isinstance(peer, allowed):
        raise ValueError('unsupported_account_search_peer')
    return peer.to_dict()


def input_peer_from_record(record):
    fields = {'InputPeerEmpty': (), 'InputPeerSelf': (), 'InputPeerUser': ('user_id', 'access_hash'),
              'InputPeerChat': ('chat_id',), 'InputPeerChannel': ('channel_id', 'access_hash')}
    kind = record.get('_') if isinstance(record, dict) else None
    if kind not in fields or set(record) != {'_', *fields[kind]}:
        raise ValueError('invalid_account_search_peer_cursor')
    if any(type(record[k]) is not int for k in fields[kind]):
        raise ValueError('invalid_account_search_peer_cursor')
    return getattr(types, kind)(**{k: record[k] for k in fields[kind]})


class AccountSearchBackend:
    def __init__(self, runtime, registry, cursor_dir, converter):
        self.runtime, self.registry, self.converter = runtime, registry, converter
        self.cursors = SearchCursors(cursor_dir)
        self.lock, self.retry_at = asyncio.Lock(), 0.0

    def policy(self, agent):
        profile = self.registry.profile(agent)
        check_action(profile, 'search')
        if not profile.get('all_chats', False):
            raise AccessDenied('account_search_requires_all_chats')
        if self.runtime.is_chat_allowlist_enabled():
            raise AccessDenied('account_search_requires_unrestricted_backend')
        return profile

    def binding(self, profile, query, limit):
        return self.cursors.binding(profile, ENGINE + '\0' + query, limit)

    async def page(self, agent, query, cursor=None, limit=50, account=None):
        validate_query(query)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid_page_size')
        self.policy(agent)  # Before any cursor data or Telegram request.
        async with self.lock:
            binding = self.binding(self.policy(agent), query, limit)
            if cursor:
                state = self.cursors.load(cursor, binding)
                if state.get('response') is not None:
                    return dict(state['response'], cached_replay=True, telegram_rpc_calls=0)
                state = copy.deepcopy(state)
            else:
                state = {'binding': binding, 'engine': ENGINE,
                         'expires_at': int(time.time()) + self.cursors.ttl,
                         'max_date': int(time.time()) + 1, 'offset_rate': 0, 'offset_id': 0,
                         'offset_peer': {'_': 'InputPeerEmpty'}, 'seen': [], 'examined': 0}
                cursor = self.cursors.save(state)
            remaining = self.retry_at - time.monotonic()
            if remaining > 0:
                return {'error': 'telegram_cooldown', 'retry_after_seconds': int(remaining) + 1,
                        'next_cursor': cursor}
            client = self.runtime.get_client(account)
            rpc_count = 0

            async def rpc(request):
                nonlocal rpc_count
                if self.binding(self.policy(agent), query, limit) != binding:
                    raise AccessDenied('search_cursor_scope_changed_or_expired')
                rpc_count += 1
                # Per-request override: do not sleep inside a 120-second tunnel call.
                result = await asyncio.wait_for(client(request, flood_sleep_threshold=0), timeout=20)
                if self.binding(self.policy(agent), query, limit) != binding:
                    raise AccessDenied('search_cursor_scope_changed_or_expired')
                return result

            try:
                result = await rpc(functions.messages.SearchGlobalRequest(
                    q=query, filter=types.InputMessagesFilterEmpty(), min_date=None,
                    max_date=datetime.fromtimestamp(state['max_date'], timezone.utc),
                    offset_rate=state['offset_rate'], offset_id=state['offset_id'],
                    offset_peer=input_peer_from_record(state['offset_peer']), limit=limit,
                    # Default all account dialogs; never channels.searchPosts or broadcasts_only.
                    broadcasts_only=None, groups_only=None, users_only=None, folder_id=None))
                raw = getattr(result, 'messages', None)
                if not isinstance(raw, list) or len(raw) > 100:
                    raise RuntimeError('unexpected_account_search_shape')
                entities = {utils.get_peer_id(e): e for e in [*getattr(result, 'users', []),
                            *getattr(result, 'chats', [])] if isinstance(e, (types.User, types.Chat, types.Channel))}
                peers = {}
                for message in raw:
                    peer = getattr(message, 'peer_id', None)
                    if not isinstance(peer, (types.PeerUser, types.PeerChat, types.PeerChannel)):
                        continue
                    ident = utils.get_peer_id(peer)
                    entity = entities.get(ident)
                    if entity is None or getattr(entity, 'min', False):
                        continue
                    try:
                        peers[ident] = utils.get_input_peer(entity)
                    except (TypeError, ValueError):
                        continue
                # Verify just the peers on this page, not thousands of account dialogs.
                verified = set()
                if peers:
                    dialogs = await rpc(functions.messages.GetPeerDialogsRequest(
                        peers=[types.InputDialogPeer(p) for p in peers.values()]))
                    for dialog in getattr(dialogs, 'dialogs', []):
                        if not isinstance(dialog, types.Dialog):
                            continue
                        ident = utils.get_peer_id(dialog.peer)
                        entity = entities.get(ident)
                        if (ident in peers and dialog.top_message > 0
                                and getattr(dialog, 'folder_id', None) in (None, 0, 1)
                                and not getattr(entity, 'left', False)
                                and not getattr(entity, 'deactivated', False)):
                            verified.add(ident)
                records, seen, ordered_seen = [], set(state['seen']), list(state['seen'])
                last, consumed, size, omitted, unresolved = None, 0, 0, 0, 0
                for message in raw:
                    peer = getattr(message, 'peer_id', None)
                    ident = utils.get_peer_id(peer) if isinstance(peer, (types.PeerUser, types.PeerChat, types.PeerChannel)) else None
                    if ident not in peers or type(getattr(message, 'id', None)) is not int or message.id <= 0:
                        # Cannot safely advance an unresolvable peer. Do not declare full coverage.
                        omitted += 1; unresolved += 1; consumed += 1
                        continue
                    pair = str(ident) + ':' + str(message.id)
                    record = None
                    if is_sensitive_peer(ident):
                        omitted += 1
                    elif ident in verified and pair not in seen and not isinstance(message, types.MessageEmpty):
                        finish = getattr(message, '_finish_init', None)
                        if finish:
                            finish(client, entities, None)
                        entity = entities[ident]
                        record = dict(self.converter(message, ident, bool(getattr(entity, 'forum', False))),
                                      chat_id=ident, chat_name=self.runtime.sanitize_name(
                                          getattr(entity, 'title', None) or self.runtime.person_name(entity) or str(ident)))
                        record = redact_tokens(json.loads(json.dumps(record, ensure_ascii=False, default=str)))
                        cost = len(json.dumps(record, ensure_ascii=False).encode())
                        if size + cost > PAGE_BYTES:
                            # next_rate belongs to the complete server page. Never invent
                            # an intermediate cursor that could skip matching messages.
                            raise ValueError('account_search_page_exceeds_byte_limit')
                        records.append(record); size += cost
                    elif ident not in verified:
                        omitted += 1
                    if pair not in seen:
                        seen.add(pair); ordered_seen.append(pair)
                    consumed += 1
                    last = (message, peers[ident])
                has_more = bool(raw) and not isinstance(result, types.messages.Messages)
                new_state = {k: v for k, v in state.items() if k != 'response'}
                new_state.update(seen=ordered_seen[-4096:], examined=state['examined'] + consumed)
                if has_more:
                    if last is None:
                        raise RuntimeError('account_search_non_progressing_page')
                    message, peer = last
                    if not raw or last[0] is not raw[-1]:
                        raise RuntimeError('account_search_unresolvable_page_boundary')
                    rate = getattr(result, 'next_rate', None)
                    if rate is None:
                        date = getattr(message, 'date', None)
                        if not isinstance(date, datetime):
                            raise RuntimeError('account_search_offset_date_missing')
                        rate = int(date.timestamp())
                    new_state.update(offset_rate=rate, offset_id=message.id, offset_peer=input_peer_record(peer))
                    if all(new_state[k] == state[k] for k in ('offset_rate', 'offset_id', 'offset_peer')):
                        raise RuntimeError('account_search_non_progressing_page')
                if self.binding(self.policy(agent), query, limit) != binding:
                    raise AccessDenied('search_cursor_scope_changed_or_expired')
                next_cursor = self.cursors.save(new_state) if has_more else None
                inexact = bool(getattr(result, 'inexact', False)) or bool(unresolved)
                response = {'messages': records, 'next_cursor': next_cursor,
                    'complete': not has_more and not inexact, 'scan_finished': not has_more,
                    'partial': has_more or inexact, 'phase': 'search' if has_more else 'done',
                    'search_engine': ENGINE, 'catalog_chat_count': 0, 'chats_completed': 0,
                    'verified_chat_ids': sorted({r['chat_id'] for r in records}),
                    'messages_examined': consumed, 'messages_examined_total': new_state['examined'],
                    'omitted_records': omitted, 'telegram_rpc_calls': rpc_count,
                    'snapshot_started_at': state['max_date'] - 1,
                    'snapshot_kind': 'telegram_search_index_date_bound',
                    'coverage': 'current_account_dialogs_except_sensitive_chat',
                    'inexact': inexact, 'untrusted_content': True, 'cached_replay': False}
                state['response'] = response; self.cursors.save(state, cursor)
                return response
            except AccessDenied:
                raise
            except Exception as exc:
                seconds = getattr(exc, 'seconds', None)
                if type(seconds) is int and seconds > 0:
                    self.retry_at = time.monotonic() + seconds
                    return {'error': 'telegram_cooldown', 'retry_after_seconds': seconds + 1,
                            'next_cursor': cursor}
                if isinstance(exc, ValueError) and str(exc) == 'account_search_page_exceeds_byte_limit':
                    return {'error': str(exc), 'next_cursor': cursor}
                # Never log query, peer routing metadata or source message contents.
                return {'error': 'account_search_page_failed', 'next_cursor': cursor}


def register_account_search(runtime):
    from .config import private_dir
    from telegram_mcp.tools.messages import message_to_dict
    service = AccountSearchBackend(runtime, AgentRegistry(private_dir() / 'agents.json'),
                                    private_dir() / 'account-search-cursors', message_to_dict)

    @runtime.mcp.tool(annotations=runtime.ToolAnnotations(
        title='Read scoped full-account search page', readOnlyHint=True, openWorldHint=True))
    @runtime.with_account(readonly=True)
    async def read_account_search_page(agent_id: str, query: str, cursor: Optional[str] = None,
                                        limit: int = 50, account: Optional[str] = None) -> dict:
        """Private backend: requires this registered agent's search and all_chats=true.

        At most one bounded search RPC and one page-local dialog verification RPC.
        Opaque agent/query/scope-bound cursors persist offsets without prefix refetch.
        No arbitrary public post search, read receipts, media or mutation.
        """
        return await service.page(agent_id, query, cursor, limit, account)
