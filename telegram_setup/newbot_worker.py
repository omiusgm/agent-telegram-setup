"""Fixed BotFather creation worker using the existing shared Telegram client.

Only explicit, locally recorded operator assertions are accepted. They are not
proof of the identity of the person behind an agent. No generic chat operation,
history read, token output, token rotation or bot deletion is provided.
"""
import asyncio
import logging
import re

from telethon import events, functions, types

from .agents import AccessDenied, check_action
from .newbot_protocol import newbot_plan
from .newbot_store import NewBotClaims, OperatorApprovals, public_receipt
from .newbot_token_files import FileBotTokenStore
from .owned_inventory import _active_usernames, _bot_entities
from .sensitive_peer import BOTFATHER_ID

_NAME_PROMPT = re.compile(r'(?is)\bnew bot\b.*\b(?:choose|pick) a name for your bot\b')
_USERNAME_PROMPT = re.compile(r'(?is)\bchoose a username for your bot\b.*\bend in\b.*\bbot\b')
_TOKEN = re.compile(r'(?<![0-9])([1-9][0-9]{4,14}:[A-Za-z0-9_-]{30,})(?![A-Za-z0-9_-])')
_SUCCESS = re.compile(r'(?is)\bdone!\s+congratulations on your new bot\b')


def logging_is_quiet():
    names = ['telethon'] + [name for name in logging.root.manager.loggerDict if name.startswith('telethon.')]
    return all(logging.getLogger(name).getEffectiveLevel() >= logging.WARNING for name in names)


def classified(message, expected, sent_id, username):
    """Return a classified local result. Raw response text never leaves this function."""
    if (getattr(message, 'chat_id', None) != BOTFATHER_ID
            or getattr(message, 'sender_id', None) != BOTFATHER_ID
            or getattr(message, 'out', False) is not False
            or type(getattr(message, 'id', None)) is not int or message.id <= sent_id):
        raise ValueError('unexpected_botfather_response')
    reply = getattr(message, 'reply_to', None)
    reply_id = getattr(reply, 'reply_to_msg_id', None) if reply is not None else None
    if reply_id is not None and reply_id != sent_id: raise ValueError('unexpected_botfather_reply_target')
    text = getattr(message, 'raw_text', None)
    if not isinstance(text, str) or len(text) > 16384: raise ValueError('unexpected_botfather_response')
    if expected == 'name' and _NAME_PROMPT.search(text) and not _TOKEN.search(text): return None
    if expected == 'username' and _USERNAME_PROMPT.search(text) and not _TOKEN.search(text): return None
    if expected == 'created' and _SUCCESS.search(text):
        # Require the exact selected public username in the success reply.
        if not re.search(r'(?i)(?:https?://)?t\.me/' + re.escape(username) + r'(?![A-Za-z0-9_])', text):
            raise ValueError('unexpected_botfather_response')
        found = _TOKEN.findall(text)
        if len(found) == 1: return found[0]
    raise ValueError('unexpected_botfather_response')


class NewBotWorker:
    def __init__(self, client, registry, folder, *, barrier=None, timeout=10):
        self.client, self.registry, self.folder = client, registry, folder
        self.approvals = OperatorApprovals(folder / 'newbot-approvals')
        self.claims = NewBotClaims(folder / 'newbot-claims')
        self.vault = FileBotTokenStore(folder / 'bot-tokens')
        self.barrier = barrier
        self.timeout = timeout

    def policy(self, agent, mode=None):
        profile = self.registry.profile(agent)
        check_action(profile, 'create-owned-bot')
        current = profile.get('bot_create_mode', 'operator')
        if mode is not None and current != mode:
            raise AccessDenied('bot_creation_mode_changed')
        return current

    def authorizer(self, agent, mode):
        if mode == 'operator': return self.approvals
        if mode != 'delegated': raise AccessDenied('invalid_bot_creation_mode')
        worker = self
        class DelegatedAgentPermission:
            def consume(self, actor, name, username, request_id):
                # This verifies the fixed agent's configured authority, not
                # whether the triggering content came from a human owner.
                if actor != agent: raise AccessDenied('agent_mismatch')
                newbot_plan(name, username, request_id)
                worker.policy(agent, 'delegated')
        return DelegatedAgentPermission()

    async def call(self, request):
        return await asyncio.wait_for(self.client(request, flood_sleep_threshold=0), self.timeout)

    async def validate_target(self, username):
        owner = await asyncio.wait_for(self.client.get_me(), self.timeout)
        entity = await asyncio.wait_for(self.client.get_entity('BotFather'), self.timeout)
        if (getattr(owner, 'bot', True) or type(getattr(owner, 'id', None)) is not int
                or not isinstance(entity, types.User) or entity.id != BOTFATHER_ID
                or not getattr(entity, 'bot', False) or not getattr(entity, 'verified', False)
                or (getattr(entity, 'username', '') or '').casefold() != 'botfather'):
            raise AccessDenied('verified_botfather_unavailable')
        owned = _bot_entities(await self.call(functions.bots.GetAdminedBotsRequest()))
        if any(username.casefold() in {u.casefold() for u in _active_usernames(bot)} for bot in owned):
            raise ValueError('bot_username_already_owned')
        return entity

    async def create(self, agent, name, username, request_id):
        newbot_plan(name, username, request_id); mode = self.policy(agent)
        # The shared-backend barrier rejects creation while another MCP call
        # is in flight and prevents new calls while this short exchange runs.
        if self.barrier is None: raise PermissionError('newbot_barrier_required')
        if not logging_is_quiet(): raise PermissionError('telethon_debug_logging_enabled')
        self.barrier.enter()
        try:
            self.policy(agent, mode)
            with self.claims as claims:
                # A previously claimed request is returned without opening the
                # BotFather dialog or touching Telegram. No second send occurs.
                replay = claims.find(agent, name, username, request_id)
                if replay is not None: return public_receipt(replay)
                entity = await self.validate_target(username)
                self.policy(agent, mode)
                interrupted = asyncio.Event()
                async def outside_message(event):
                    if getattr(event, 'chat_id', None) == BOTFATHER_ID and getattr(event, 'out', False):
                        interrupted.set()
                event_filter = events.NewMessage(chats=BOTFATHER_ID, outgoing=True)
                self.client.add_event_handler(outside_message, event_filter)
                try:
                    async with self.client.conversation(entity, timeout=self.timeout,
                            total_timeout=45, max_messages=10, exclusive=True) as conversation:
                        claim, fresh = claims.begin(agent, name, username, request_id,
                                                    self.authorizer(agent, mode), authorization_mode=mode)
                        if not fresh: return public_receipt(claim)
                        try:
                            async def exchange(text, expected):
                                self.policy(agent, mode)
                                if interrupted.is_set() or not logging_is_quiet():
                                    raise RuntimeError('newbot_interrupted')
                                async def dispatch():
                                    self.policy(agent, mode)
                                    if interrupted.is_set(): raise RuntimeError('newbot_interrupted')
                                    return await conversation.send_message(text, parse_mode=None)
                                sent = await asyncio.wait_for(dispatch(), self.timeout)
                                if (type(getattr(sent, 'id', None)) is not int or sent.id <= 0
                                        or interrupted.is_set()): raise RuntimeError('newbot_send_uncertain')
                                reply = await asyncio.wait_for(conversation.get_response(sent, timeout=self.timeout), self.timeout)
                                if interrupted.is_set(): raise RuntimeError('newbot_interrupted')
                                return classified(reply, expected, sent.id, username)
                            await exchange('/newbot', 'name')
                            await exchange(name, 'username')
                            token = await exchange(username, 'created')
                            bot_id = int(token.split(':', 1)[0])
                            token_ref = self.vault.for_intent(claim['token_intent']).store(token)
                            if token_ref != claim['reserved_ref']:
                                raise RuntimeError('token_reference_mismatch')
                            token = None
                            self.policy(agent, mode)
                            if interrupted.is_set(): raise RuntimeError('newbot_interrupted')
                            owned = _bot_entities(await self.call(functions.bots.GetAdminedBotsRequest()))
                            if not any(getattr(bot, 'id', None) == bot_id
                                    and username.casefold() in {u.casefold() for u in _active_usernames(bot)}
                                    for bot in owned):
                                raise RuntimeError('newbot_ownership_not_verified')
                            completed = claims.finish_verified(claim, bot_id, username)
                            return public_receipt(completed)
                        except Exception:
                            # A command may have reached Telegram; preserve the
                            # durable pending claim and never log the response.
                            return public_receipt(claim)
                finally:
                    self.client.remove_event_handler(outside_message, event_filter)
        finally:
            self.barrier.exit()
