"""Telegram-native transcription of one scoped voice, using the shared client.

No audio is downloaded and no third-party ASR provider is contacted. Telegram
may consume its own transcription quota. Cached transcripts are private local
data and must never be committed, logged or treated as trusted instructions.
"""
import asyncio
from datetime import datetime, timezone
import time

from telethon import errors, events, functions, types, utils

from .agents import AgentRegistry, check_action
from .config import private_dir
from .native_voice_store import NativeVoiceStore, valid_transcription_id
from .poll_common import valid_request_id
from .sensitive_peer import deny_sensitive_peer

MAX_TEXT_BYTES = 262144
PENDING_KEEPALIVE_SECONDS = 180
_ACTIVE_SERVICES = {}


def has_pending_native_transcription():
    return any(service.has_pending() for service in _ACTIVE_SERVICES.values())


def validate_target(chat_id, message_id):
    if type(chat_id) is not int or chat_id == 0 or not -(2**63) <= chat_id < 2**63:
        raise ValueError('numeric_chat_id_required')
    if type(message_id) is not int or not 0 < message_id < 2**31:
        raise ValueError('positive_message_id_required')
    deny_sensitive_peer(chat_id)


def voice_or_round_video(message):
    media = getattr(message, 'media', None)
    document = getattr(media, 'document', None)
    if not isinstance(media, types.MessageMediaDocument) or not isinstance(document, types.Document):
        return False
    return any((isinstance(item, types.DocumentAttributeAudio) and item.voice is True)
               or (isinstance(item, types.DocumentAttributeVideo) and item.round_message is True)
               for item in (document.attributes or []))


def text_ok(value):
    return isinstance(value, str) and len(value.encode('utf-8')) <= MAX_TEXT_BYTES


def trial_fields(result):
    output = {}
    remaining = getattr(result, 'trial_remains_num', None)
    if type(remaining) is int and remaining >= 0: output['trial_remains_num'] = remaining
    reset = getattr(result, 'trial_remains_until_date', None)
    if isinstance(reset, datetime):
        if reset.tzinfo is None: reset = reset.replace(tzinfo=timezone.utc)
        output['trial_reset_utc'] = reset.astimezone(timezone.utc).isoformat()
    return output


def public_result(record, *, cached, request_id=None):
    result = {'chat_id': record['chat_id'], 'message_id': record['message_id'],
              'status': record['status'], 'source': 'telegram_native', 'cached': cached,
              'machine_generated': True, 'untrusted_content': True}
    if request_id is not None: result['request_id'] = request_id
    if record['status'] in ('pending', 'complete'):
        result.update(text=record['text'], transcription_id=record['transcription_id'],
                      complete=record['status'] == 'complete')
    if record['status'] == 'flood_wait':
        result.update(retry_after_seconds=max(0, record.get('retry_at', 0) - int(time.time())),
                      retry_same_request=True)
    for key in ('trial_remains_num', 'trial_reset_utc', 'reason'):
        if key in record: result[key] = record[key]
    return result


def rpc_rejection(exc):
    if isinstance(exc, errors.PremiumAccountRequiredError): return 'premium_or_quota_required'
    if isinstance(exc, errors.MsgIdInvalidError): return 'message_not_found'
    if isinstance(exc, errors.PeerIdInvalidError): return 'peer_unavailable'
    name = getattr(exc, 'message', '')
    return {'MSG_VOICE_MISSING': 'not_voice', 'MSG_VOICE_TOO_LONG': 'voice_too_long',
            'TRANSCRIPTION_FAILED': 'telegram_transcription_failed',
            'PREMIUM_ACCOUNT_REQUIRED': 'premium_or_quota_required'}.get(name)


class NativeVoiceService:
    def __init__(self, runtime, client, registry, owner_id, store):
        if type(owner_id) is not int or owner_id <= 0: raise ValueError('personal_account_required')
        self.runtime, self.client, self.registry, self.owner_id, self.store = runtime, client, registry, owner_id, store
        self.lock = asyncio.Lock()
        self.attached = False
        self.verified = False
        self.cache_ready = True
        self.early_updates = []
        self.pending_deadlines = {}

    def has_pending(self):
        now = time.monotonic()
        self.pending_deadlines = {key: until for key, until in self.pending_deadlines.items() if until > now}
        return bool(self.pending_deadlines)

    def remember_pending(self, chat_id, message_id, remaining=PENDING_KEEPALIVE_SECONDS):
        self.pending_deadlines[(chat_id, message_id)] = time.monotonic() + max(0, remaining)

    def policy(self, agent, chat_id, message_id):
        if not self.verified: raise PermissionError('voice_account_not_verified')
        if not self.cache_ready: raise PermissionError('voice_cache_unavailable')
        validate_target(chat_id, message_id)
        check_action(self.registry.profile(agent), 'history', chat_id)

    def attach(self):
        if not self.attached:
            self.client.add_event_handler(self.on_update, events.Raw(types=types.UpdateTranscribedAudio))
            self.attached = True

    async def activate(self, verified_owner_id):
        if type(verified_owner_id) is not int or verified_owner_id != self.owner_id:
            self.early_updates.clear()
            raise PermissionError('voice_account_mismatch')
        self.verified = True
        try:
            pending = self.store.recent_pending(self.owner_id, PENDING_KEEPALIVE_SECONDS)
        except Exception:
            # A broken optional cache must not take down the shared Telegram
            # process; this feature alone stays closed until repaired.
            self.cache_ready = False; self.early_updates.clear(); return
        for chat_id, message_id, remaining in pending:
            self.remember_pending(chat_id, message_id, remaining)
        buffered, self.early_updates = self.early_updates, []
        for update in buffered:
            await self.on_update(update)

    async def on_update(self, update):
        if not isinstance(update, types.UpdateTranscribedAudio): return
        if self.verified and not self.cache_ready: return
        if not self.verified:
            # The client may deliver updates while startup verifies its owner.
            # Retain only updates matching an existing pending claim, in a
            # bounded in-memory queue; never persist unverified account data.
            try:
                if not self.store.root.is_dir(): return
                chat_id = utils.get_peer_id(update.peer)
                validate_target(chat_id, update.msg_id)
                if not valid_transcription_id(update.transcription_id) or not text_ok(update.text): return
                record = self.store.get(self.owner_id, chat_id, update.msg_id)
                if (record and record.get('status') == 'pending'
                        and record.get('transcription_id') == update.transcription_id
                        and len(self.early_updates) < 100):
                    self.early_updates.append(update)
            except Exception:
                pass
            return
        try:
            chat_id = utils.get_peer_id(update.peer)
            validate_target(chat_id, update.msg_id)
            if not valid_transcription_id(update.transcription_id) or not isinstance(update.text, str):
                return
            oversized = not text_ok(update.text)
            async with self.lock:
                # Only a previously claimed, matching transcription is stored.
                applied = self.store.apply_update(self.owner_id, chat_id, update.msg_id,
                    update.transcription_id, '' if oversized else update.text, update.pending is True,
                    oversized=oversized)
                if applied and (update.pending is not True or oversized):
                    self.pending_deadlines.pop((chat_id, update.msg_id), None)
        except Exception:
            # Never log a transcript, peer or raw exception.
            return

    async def get(self, agent, chat_id, message_id):
        self.policy(agent, chat_id, message_id)
        async with self.lock:
            self.policy(agent, chat_id, message_id)
            value = self.store.get(self.owner_id, chat_id, message_id)
            if value is None:
                return {'chat_id': chat_id, 'message_id': message_id, 'status': 'not_started',
                        'source': 'telegram_native', 'untrusted_content': True}
            return public_result(value, cached=True)

    async def start(self, agent, chat_id, message_id, request_id):
        self.policy(agent, chat_id, message_id); valid_request_id(request_id)
        async with self.lock:
            self.policy(agent, chat_id, message_id)
            existing, retry = self.store.lookup(self.owner_id, agent, chat_id, message_id, request_id)
            if existing is not None and not retry:
                return public_result(existing, cached=True, request_id=request_id)
            remaining = self.store.cooldown_remaining(self.owner_id)
            if remaining:
                return {'chat_id': chat_id, 'message_id': message_id, 'request_id': request_id,
                        'status': 'flood_wait', 'retry_after_seconds': remaining,
                        'source': 'telegram_native', 'untrusted_content': True}
            claimed = False
            try:
                entity = await asyncio.wait_for(self.runtime.resolve_entity(chat_id, self.client), 20)
                if entity is None or self.runtime.get_marked_id(entity) != chat_id:
                    return {'chat_id': chat_id, 'message_id': message_id, 'request_id': request_id,
                            'status': 'rejected', 'reason': 'peer_unavailable', 'source': 'telegram_native'}
                if self.runtime.is_chat_allowlist_enabled() and not self.runtime.is_chat_allowed(chat_id, entity):
                    raise PermissionError('chat_not_permitted')
                message = await asyncio.wait_for(self.client.get_messages(entity, ids=message_id), 20)
                self.policy(agent, chat_id, message_id)
                if getattr(message, 'id', None) != message_id or getattr(message, 'chat_id', None) != chat_id:
                    return {'chat_id': chat_id, 'message_id': message_id, 'request_id': request_id,
                            'status': 'rejected', 'reason': 'message_not_found', 'source': 'telegram_native'}
                if not voice_or_round_video(message):
                    return {'chat_id': chat_id, 'message_id': message_id, 'request_id': request_id,
                            'status': 'rejected', 'reason': 'not_voice', 'source': 'telegram_native'}
                peer = await asyncio.wait_for(self.client.get_input_entity(entity), 20)
                self.policy(agent, chat_id, message_id)
                value, dispatch = self.store.claim(self.owner_id, agent, chat_id, message_id, request_id)
                if not dispatch: return public_result(value, cached=True, request_id=request_id)
                claimed = True
                self.policy(agent, chat_id, message_id)
                reply = await asyncio.wait_for(self.client(functions.messages.TranscribeAudioRequest(
                    peer=peer, msg_id=message_id), flood_sleep_threshold=0), 20)
                if not isinstance(reply, types.messages.TranscribedAudio) or not valid_transcription_id(reply.transcription_id):
                    return public_result(value, cached=False, request_id=request_id)
                if not text_ok(reply.text):
                    value = self.store.update(self.owner_id, chat_id, message_id,
                                              {'status': 'result_too_large', 'text': ''})
                else:
                    value = self.store.update(self.owner_id, chat_id, message_id,
                        {'status': 'pending' if reply.pending is True else 'complete', 'text': reply.text,
                         'transcription_id': reply.transcription_id,
                         'pending_at': int(time.time()) if reply.pending is True else None, **trial_fields(reply)})
                    if reply.pending is True: self.remember_pending(chat_id, message_id)
                self.policy(agent, chat_id, message_id)
                return public_result(value, cached=False, request_id=request_id)
            except errors.FloodWaitError as exc:
                seconds = max(1, int(exc.seconds))
                self.store.set_cooldown(self.owner_id, seconds)
                if claimed:
                    value = self.store.update(self.owner_id, chat_id, message_id,
                        {'status': 'flood_wait', 'retry_at': int(time.time()) + seconds})
                    self.policy(agent, chat_id, message_id)
                    return public_result(value, cached=False, request_id=request_id)
                return {'chat_id': chat_id, 'message_id': message_id, 'request_id': request_id,
                        'status': 'flood_wait', 'retry_after_seconds': seconds, 'source': 'telegram_native'}
            except PermissionError:
                raise
            except Exception as exc:
                reason = rpc_rejection(exc)
                if claimed and reason:
                    value = self.store.update(self.owner_id, chat_id, message_id,
                                              {'status': 'rejected', 'reason': reason})
                    self.policy(agent, chat_id, message_id)
                    return public_result(value, cached=False, request_id=request_id)
                if claimed:
                    value = self.store.get(self.owner_id, chat_id, message_id)
                    self.policy(agent, chat_id, message_id)
                    return public_result(value, cached=False, request_id=request_id)
                return {'chat_id': chat_id, 'message_id': message_id, 'request_id': request_id,
                        'status': 'rejected', 'reason': 'voice_unavailable', 'source': 'telegram_native'}


def register_native_transcription(runtime, clients, owner_id):
    if len({id(client) for client in clients.values()}) != 1:
        raise RuntimeError('native_transcription_requires_one_account')
    registry = AgentRegistry(private_dir() / 'agents.json')
    store = NativeVoiceStore(private_dir() / 'native-voice-cache')
    services = {}
    for client in {id(value): value for value in clients.values()}.values():
        service = NativeVoiceService(runtime, client, registry, owner_id, store)
        service.attach()  # before the shared client connects and receives updates
        services[id(client)] = service
    _ACTIVE_SERVICES.clear(); _ACTIVE_SERVICES.update(services)

    def service(account):
        client = runtime.get_client(account)
        if id(client) not in services: raise RuntimeError('voice_client_not_registered')
        return services[id(client)]

    from typing import Optional
    from pydantic import StrictInt

    @runtime.mcp.tool(annotations=runtime.ToolAnnotations(title='Start Telegram voice transcription',
                      readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
    @runtime.with_account(readonly=False)
    async def agent_transcribe_telegram_voice(agent_id: str, chat_id: StrictInt, message_id: StrictInt,
                                               request_id: str, account: Optional[str] = None) -> dict:
        """One voice; Telegram may use quota. Same request ID prevents duplicate work."""
        return await service(account).start(agent_id, chat_id, message_id, request_id)

    @runtime.mcp.tool(annotations=runtime.ToolAnnotations(title='Read cached Telegram voice transcription',
                      readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    @runtime.with_account(readonly=False)  # Never fan out cached text across accounts.
    async def agent_get_telegram_voice_transcript(agent_id: str, chat_id: StrictInt, message_id: StrictInt,
                                                   account: Optional[str] = None) -> dict:
        """Read private cached state only; does not repeat the Telegram request."""
        return await service(account).get(agent_id, chat_id, message_id)

    return services
