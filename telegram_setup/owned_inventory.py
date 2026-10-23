"""Read-only inventory of chats and bots currently owned by the account."""
from datetime import datetime, timezone
import math
import time
from typing import Optional

from telethon.errors import FloodWaitError


_INVENTORIES = {}
_INVENTORY_LOCKS = {}
_DIALOGS_PER_CALL = 200


def has_pending_inventory():
    """Keep the account process alive while a cursor or FloodWait is pending."""
    return bool(_INVENTORIES)


def _marked_id(entity):
    if entity is None:
        return None
    try:
        from telethon.utils import get_peer_id
        return int(get_peer_id(entity))
    except Exception:
        value = getattr(entity, 'id', None)
        return int(value) if isinstance(value, int) else None


def _active_usernames(entity):
    values = []
    primary = getattr(entity, 'username', None)
    if isinstance(primary, str) and primary:
        values.append(primary)
    for item in (getattr(entity, 'usernames', None) or []):
        value = getattr(item, 'username', None)
        if (isinstance(value, str) and value
                and getattr(item, 'active', False)):
            values.append(value)
    return list(dict.fromkeys(values))


def _bot_entities(result):
    """Extract only bot User entities from bots.getAdminedBots output."""
    users = []
    ids = set()

    def visit(value):
        if value is None:
            return
        if isinstance(value, (list, tuple, set)):
            for part in value:
                visit(part)
            return
        user_id = getattr(value, 'bot_id', None)
        if not isinstance(user_id, int):
            user_id = getattr(value, 'user_id', None)
        if isinstance(user_id, int):
            ids.add(user_id)
        candidate_id = getattr(value, 'id', None)
        if isinstance(candidate_id, int) and getattr(value, 'bot', False):
            users.append(value)
        for field in ('users', 'bots', 'items'):
            nested = getattr(value, field, None)
            if nested is not None and nested is not value:
                visit(nested)

    visit(result)
    by_id = {getattr(user, 'id', None): user for user in users}
    # Some Telethon layers return the bot IDs separately from the User vector.
    return [by_id[i] for i in sorted(ids) if i in by_id] or users


def register_owned_inventory(runtime):
    from mcp.types import ToolAnnotations
    from telethon.tl.functions.bots import GetAdminedBotsRequest
    from telethon.tl.types import Chat, Channel

    @runtime.mcp.tool(annotations=ToolAnnotations(
        title='Read currently owned Telegram chats and bots',
        readOnlyHint=True,
        openWorldHint=True,
    ))
    @runtime.with_account(readonly=True)
    async def read_owned_inventory(account: Optional[str] = None, cancel: bool = False) -> dict:
        """List creator-marked chats and bots returned by Telegram's owned-bots API.

        Reads the current account only. It does not inspect message contents,
        bot tokens, authentication material, or access hashes. Repeat calls until
        complete=true; wait retry_after_seconds before retrying when it is positive.
        Progress responses are partial and may be saved only as checkpoints.
        cancel=true discards only the partial local scan; no Telegram writes.
        """
        client = runtime.get_client(account)
        state_key = (account, id(client))
        lock = _INVENTORY_LOCKS.setdefault(state_key, __import__('asyncio').Lock())
        async with lock:
            if cancel:
                _INVENTORIES.pop(state_key, None)
                return {'cancelled': True, 'complete': False, 'telegram_writes': 0}
            state = _INVENTORIES.get(state_key)
            if state is None:
                state = {
                    'iterator': client.iter_dialogs(limit=None, archived=None),
                    'dialogs_complete': False,
                    'dialogs_total': 0,
                    'group_channel_total': 0,
                    'allowlist_filtered': 0,
                    'current_chats': [],
                    'historical_chats': [],
                    'bot_dialogs': {},
                    'owned_bots': [],
                    'not_before': 0.0,
                }
                _INVENTORIES[state_key] = state
            remaining = state['not_before'] - time.monotonic()
            if remaining > 0:
                return _partial_report(state, math.ceil(remaining))
            try:
                await runtime.ensure_connected(client)
            except FloodWaitError as exc:
                state['not_before'] = time.monotonic() + max(0, exc.seconds)
                return _partial_report(state, exc.seconds)

            batch_count = 0
            while not state['dialogs_complete']:
                if batch_count >= _DIALOGS_PER_CALL:
                    return _partial_report(state, 0)
                try:
                    dialog = await state['iterator'].__anext__()
                except StopAsyncIteration:
                    state['dialogs_complete'] = True
                    break
                except FloodWaitError as exc:
                    state['not_before'] = time.monotonic() + max(0, exc.seconds)
                    return _partial_report(state, exc.seconds)

                batch_count += 1
                state['dialogs_total'] += 1
                entity = getattr(dialog, 'entity', None)
                if getattr(entity, 'bot', False):
                    entity_id = getattr(entity, 'id', None)
                    if isinstance(entity_id, int):
                        state['bot_dialogs'][entity_id] = bool(getattr(dialog, 'archived', False))
                if not isinstance(entity, (Chat, Channel)):
                    continue
                state['group_channel_total'] += 1
                chat_id = _marked_id(entity)
                if runtime.is_chat_allowlist_enabled() and not runtime.is_chat_allowed(chat_id, entity):
                    state['allowlist_filtered'] += 1
                    continue
                if not getattr(entity, 'creator', False):
                    continue

                migrated_to_entity = getattr(entity, 'migrated_to', None)
                migrated_to = _marked_id(migrated_to_entity)
                left = bool(getattr(entity, 'left', False))
                item = {
                    'kind': 'channel' if isinstance(entity, Channel) and getattr(entity, 'broadcast', False) else 'group',
                    'title': getattr(entity, 'title', None) or getattr(dialog, 'name', None),
                    'chat_id': chat_id,
                    'username': (getattr(entity, 'username', None)
                                 if isinstance(getattr(entity, 'username', None), str) else None),
                    'usernames': _active_usernames(entity),
                    'archived': bool(getattr(dialog, 'archived', False)),
                    'creator': True,
                    'ownership_evidence': 'Telegram dialog entity creator=true',
                    'migrated_to': migrated_to,
                    'left': left,
                    'folder_eligible': not bool(migrated_to or left),
                    'status': 'historical' if migrated_to or left else 'current_owner',
                }
                (state['historical_chats'] if migrated_to or left else state['current_chats']).append(item)

            if not state.get('bots_complete', False):
                try:
                    bot_result = await client(GetAdminedBotsRequest())
                except FloodWaitError as exc:
                    state['not_before'] = time.monotonic() + max(0, exc.seconds)
                    return _partial_report(state, exc.seconds)
                for bot in _bot_entities(bot_result):
                    bot_id = getattr(bot, 'id', None)
                    if not isinstance(bot_id, int):
                        continue
                    if runtime.is_chat_allowlist_enabled() and not runtime.is_chat_allowed(bot_id, bot):
                        state['allowlist_filtered'] += 1
                        continue
                    usernames = _active_usernames(bot)
                    deleted = bool(getattr(bot, 'deleted', False))
                    has_dialog = bot_id in state['bot_dialogs']
                    state['owned_bots'].append({
                        'kind': 'bot',
                        'title': getattr(bot, 'first_name', None) or getattr(bot, 'name', None),
                        'chat_id': bot_id,
                        'username': usernames[0] if usernames else None,
                        'usernames': usernames,
                        'archived': state['bot_dialogs'].get(bot_id),
                        'has_dialog': has_dialog,
                        'deleted': deleted,
                        'ownership_evidence': 'Telegram bots.getAdminedBots',
                        'folder_eligible': not deleted,
                        'status': 'currently_owned',
                    })
                state['bots_complete'] = True

            report = _report(state, complete=True)
            _INVENTORIES.pop(state_key, None)
            return report


def _report(state, *, complete, retry_after_seconds=None):
    report = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'complete': complete,
        'scope': 'Current Telegram dialogs, including archived dialogs, plus bots.getAdminedBots.',
        'counts': {
            'dialogs_scanned': state['dialogs_total'],
            'group_channel_dialogs_scanned': state['group_channel_total'],
            'current_creator_chats': len(state['current_chats']),
            'historical_creator_chats': len(state['historical_chats']),
            'currently_owned_bots': len(state['owned_bots']),
            'allowlist_filtered': state['allowlist_filtered'],
        },
        'chats': state['current_chats'],
        'historical_chats': state['historical_chats'],
        'bots': state['owned_bots'],
    }
    if not complete:
        report['retry_after_seconds'] = max(0, int(retry_after_seconds or 0))
        report['progress'] = 'dialog_scan' if not state['dialogs_complete'] else 'owned_bots'
    return report


def _partial_report(state, seconds):
    return _report(state, complete=False, retry_after_seconds=seconds)
