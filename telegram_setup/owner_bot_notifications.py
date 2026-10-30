"""On-demand bot notification to the Telegram account owner only.

The bot token remains on this host; no getUpdates/webhook/daemon is used.
This component is opt-in and not evidence of a delivered live message.
"""
import asyncio
from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import re
import ssl

from telethon import functions

from .agents import AccessDenied, check_bot_notify
from .newbot_token_files import FileBotTokenStore
from .owned_inventory import _bot_entities
from .poll_common import fingerprint, valid_request_id
from .poll_receipts import PollReceipts
from .private_state import read_private, secure_directory
from .sensitive_peer import redact_tokens

_RECEIPT_NAME = re.compile(r'[0-9a-f]{64}\.json\Z')
_HISTORY_LIMIT = 50
_HISTORY_ATTEMPTS = 8
_HISTORY_PAUSE = 0.6


def token_ref_for_verified_bot(folder, bot_id):
    root = secure_directory(folder, create=False)
    found = []
    paths = [p for p in root.iterdir() if _RECEIPT_NAME.fullmatch(p.name)]
    if len(paths) > 4096: raise ValueError('bot_receipt_capacity_exceeded')
    for path in paths:
        row = json.loads(read_private(path, 8192))
        if (isinstance(row, dict) and row.get('status') == 'created_verified'
                and row.get('readback_verified') is True and row.get('bot_id') == bot_id):
            intent, ref = row.get('token_intent'), row.get('token_ref')
            if (not isinstance(intent, str) or not isinstance(ref, str)
                    or FileBotTokenStore.reference_for_intent(intent) != ref):
                raise ValueError('invalid_verified_bot_receipt')
            found.append(ref)
    if len(found) != 1: raise AccessDenied('verified_bot_token_not_found')
    return found[0]


def bot_api_send(token, owner_id, bot_id, text):
    """Only HTTPS POST to Telegram's fixed host; exceptions never escape."""
    connection = None
    try:
        if (not isinstance(token, str) or not re.fullmatch(r'[1-9][0-9]{4,18}:[A-Za-z0-9_-]{30,128}', token)
                or int(token.split(':', 1)[0]) != bot_id):
            return {'status': 'uncertain_do_not_retry'}
        payload = json.dumps({'chat_id': owner_id, 'text': text}, ensure_ascii=False).encode()
        connection = http.client.HTTPSConnection('api.telegram.org', 443, timeout=12, context=ssl.create_default_context())
        connection.set_debuglevel(0)
        # Bot API places the credential in the request path. Never log this
        # path, the exception text, the response body, or token bytes.
        connection.request('POST', '/bot' + token + '/sendMessage', payload,
                           {'Content-Type': 'application/json', 'Accept': 'application/json'})
        response = connection.getresponse(); raw = response.read(65537)
        if len(raw) > 65536: return {'status': 'uncertain_do_not_retry'}
        value = json.loads(raw)
        if isinstance(value, dict) and value.get('ok') is True and response.status == 200:
            message = value.get('result', {})
            if (isinstance(message, dict) and type(message.get('message_id')) is int
                    and message['message_id'] > 0 and type(message.get('date')) is int and message['date'] > 0
                    and message.get('chat', {}).get('id') == owner_id
                    and message.get('from', {}).get('id') == bot_id and message.get('text') == text):
                return {'status': 'accepted_by_telegram', 'bot_message_id': message['message_id'],
                        'bot_date_unix': message['date']}
        if (isinstance(value, dict) and value.get('ok') is False
                and response.status in (400, 403, 429) and value.get('error_code') == response.status):
            safe = {'status': 'rejected_not_sent', 'error_code': response.status}
            wait = value.get('parameters', {}).get('retry_after') if isinstance(value.get('parameters'), dict) else None
            if type(wait) is int and wait > 0: safe['retry_after_seconds'] = wait
            return safe
    except Exception:
        pass
    finally:
        if connection is not None:
            try: connection.close()
            except Exception: pass
    return {'status': 'uncertain_do_not_retry'}


def _message_epoch(message):
    value = getattr(message, 'date', None)
    if not isinstance(value, datetime): return None
    if value.tzinfo is None: value = value.replace(tzinfo=timezone.utc)
    try: return int(value.timestamp())
    except (OverflowError, OSError, ValueError): return None


async def owner_baseline(client, bot, bot_id):
    """Snapshot the owner's own message namespace before Bot API dispatch."""
    rows = await asyncio.wait_for(client.get_messages(bot, limit=1), 5)
    if not isinstance(rows, (list, tuple)) or len(rows) > 1:
        raise ValueError('owner_snapshot_unavailable')
    if not rows: return 0
    message = rows[0]
    ident = getattr(message, 'id', None)
    if type(ident) is not int or ident <= 0 or getattr(message, 'chat_id', None) != bot_id:
        raise ValueError('owner_snapshot_unavailable')
    return ident


async def correlate_owner_message(client, bot, bot_id, text, baseline, bot_date_unix):
    """Find one stable peer/sender/text/date match after the owner baseline.

    Bot API and personal MTProto message IDs belong to different accounts.
    A bounded or ambiguous result is unconfirmed; never resend to resolve it.
    """
    previous = None
    for attempt in range(_HISTORY_ATTEMPTS):
        rows = await asyncio.wait_for(client.get_messages(bot, limit=_HISTORY_LIMIT + 1, min_id=baseline), 5)
        if not isinstance(rows, (list, tuple)) or len(rows) > _HISTORY_LIMIT: return None
        matches = []
        for message in rows:
            ident = getattr(message, 'id', None)
            if (type(ident) is not int or ident <= baseline or getattr(message, 'chat_id', None) != bot_id):
                return None
            if (getattr(message, 'sender_id', None) == bot_id and getattr(message, 'out', None) is False
                    and getattr(message, 'raw_text', None) == text and _message_epoch(message) == bot_date_unix):
                matches.append(ident)
        if len(matches) > 1: return None
        current = matches[0] if matches else None
        if previous is not None and current != previous: return None
        if current is not None and current == previous: return current
        previous = current
        if attempt + 1 < _HISTORY_ATTEMPTS: await asyncio.sleep(_HISTORY_PAUSE)
    return None


class OwnerBotNotifier:
    def __init__(self, client, registry, folder, *, transport=bot_api_send):
        self.client, self.registry, self.folder, self.transport = client, registry, Path(folder), transport
        self.vault = FileBotTokenStore(self.folder / 'bot-tokens')
        self.receipts = PollReceipts(self.folder / 'bot-notification-receipts', account_wide_guard=True)
        self.lock = asyncio.Lock()

    def policy(self, agent, bot_id): check_bot_notify(self.registry.profile(agent), bot_id)

    async def send(self, agent, bot_id, text, request_id):
        valid_request_id(request_id); self.policy(agent, bot_id)
        if (not isinstance(text, str) or not text.strip() or len(text.encode('utf-16-le')) // 2 > 4096
                or redact_tokens(text) != text): raise ValueError('invalid_notification_text')
        async with self.lock:
            self.policy(agent, bot_id)
            owner = await asyncio.wait_for(self.client.get_me(), 10)
            owner_id = getattr(owner, 'id', None)
            if type(owner_id) is not int or owner_id <= 0 or getattr(owner, 'bot', True):
                raise AccessDenied('owner_identity_unavailable')
            digest = fingerprint('bot-owner-notify', bot_id=bot_id, owner_id=owner_id, text=text)
            previous = self.receipts.existing(agent, request_id, digest)
            if previous is not None: return {k: v for k, v in previous.items() if k != 'fingerprint'}
            bots = _bot_entities(await asyncio.wait_for(
                self.client(functions.bots.GetAdminedBotsRequest(), flood_sleep_threshold=0), 10))
            bot = next((b for b in bots if getattr(b, 'id', None) == bot_id), None)
            if bot is None: raise AccessDenied('bot_not_owned')
            ref = token_ref_for_verified_bot(self.folder / 'newbot-claims', bot_id)
            if self.vault.metadata(ref)['token_bot_id'] != bot_id: raise AccessDenied('bot_token_identity_mismatch')
            self.policy(agent, bot_id)
            # No Bot API request is dispatched unless the exact owner↔bot
            # dialog can first be snapshotted in the owner's ID namespace.
            baseline = await owner_baseline(self.client, bot, bot_id)
            self.policy(agent, bot_id)
            path, previous = self.receipts.begin(agent, request_id, digest, bot_id, 'bot-owner-notify')
            if previous is not None: return {k: v for k, v in previous.items() if k != 'fingerprint'}
            receipt = {'request_id': request_id, 'bot_id': bot_id, 'operation': 'bot-owner-notify',
                       'status': 'uncertain_do_not_retry', 'readback_verified': False}
            try:
                self.policy(agent, bot_id)
                token = self.vault.load_for_broker(ref)
                result = await asyncio.wait_for(asyncio.to_thread(self.transport, token, owner_id, bot_id, text), 20)
                token = None
                if isinstance(result, dict) and result.get('status') == 'rejected_not_sent':
                    receipt['status'] = 'rejected_not_sent'
                    if type(result.get('error_code')) is int: receipt['error_code'] = result['error_code']
                    if type(result.get('retry_after_seconds')) is int:
                        receipt['retry_after_seconds'] = result['retry_after_seconds']
                elif (isinstance(result, dict) and result.get('status') == 'accepted_by_telegram'
                        and type(result.get('bot_message_id')) is int and result['bot_message_id'] > 0
                        and type(result.get('bot_date_unix')) is int and result['bot_date_unix'] > 0):
                    receipt.update(status='accepted_unconfirmed', bot_message_id=result['bot_message_id'])
                    self.policy(agent, bot_id)
                    owner_message_id = await asyncio.wait_for(
                        correlate_owner_message(self.client, bot, bot_id, text, baseline, result['bot_date_unix']), 8)
                    self.policy(agent, bot_id)
                    if owner_message_id is not None:
                        receipt.update(status='delivered_verified', readback_verified=True,
                                       message_id=owner_message_id, owner_message_id=owner_message_id,
                                       readback_method='stable_owner_peer_sender_text_date_after_baseline')
            except Exception:
                # No raw HTTP/SDK errors or credentials in logs/receipts.
                pass
            self.receipts.finish(path, digest, receipt)
            self.receipts.clear_guard(agent, digest, receipt)
            return receipt
