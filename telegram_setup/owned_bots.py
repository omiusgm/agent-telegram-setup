"""Owner-verified bot profiles, staged component; no BotFather dialog or tokens.

This module is inert until deliberately registered and its per-agent actions
are granted. It uses the already-running account client's MTProto methods.
"""
import asyncio

from telethon import errors, functions, utils

from .agents import AccessDenied, check_owned_bot
from .owned_inventory import _bot_entities
from .poll_common import fingerprint, valid_request_id
from .poll_receipts import PollReceipts
from .sensitive_peer import redact_tokens

PROFILE_LIMITS = {'name': 64, 'about': 120, 'description': 512}


def validate_profile(changes):
    if not isinstance(changes, dict) or not changes or not set(changes) <= PROFILE_LIMITS.keys():
        raise ValueError('invalid_profile_fields')
    for key, value in changes.items():
        if (not isinstance(value, str) or len(value.encode('utf-16-le')) // 2 > PROFILE_LIMITS[key]
                or (key == 'name' and not value.strip()) or redact_tokens(value) != value):
            raise ValueError('invalid_profile_text')
    return dict(changes)


def bot_record(bot):
    bot_id = getattr(bot, 'id', None)
    if type(bot_id) is not int or bot_id <= 0 or not getattr(bot, 'bot', False):
        raise ValueError('invalid_owned_bot')
    username = getattr(bot, 'username', None)
    name = getattr(bot, 'first_name', None)
    return redact_tokens({'bot_id': bot_id, 'username': username if isinstance(username, str) else None,
                          'name': name if isinstance(name, str) else '', 'owned': True})


class OwnedBots:
    def __init__(self, client, registry, receipt_dir):
        self.client, self.registry = client, registry
        self.receipts = PollReceipts(receipt_dir, account_wide_guard=True)
        self.lock = asyncio.Lock()

    def policy(self, agent, action, bot_id=None, *, write=False):
        check_owned_bot(self.registry.profile(agent), action, bot_id, write=write)

    async def owned(self):
        result = await self.client(functions.bots.GetAdminedBotsRequest(), flood_sleep_threshold=0)
        bots = _bot_entities(result)
        if len(bots) > 1000:
            raise ValueError('owned_bot_count_exceeded')
        return {bot_record(bot)['bot_id']: bot for bot in bots}

    async def list(self, agent):
        self.policy(agent, 'read-owned-bots')
        async with self.lock:
            self.policy(agent, 'read-owned-bots')
            bots = await self.owned()
            self.policy(agent, 'read-owned-bots')
            return {'bots': [bot_record(bots[i]) for i in sorted(bots)], 'untrusted_content': True}

    async def get(self, agent, bot_id):
        self.policy(agent, 'read-owned-bots', bot_id)
        async with self.lock:
            self.policy(agent, 'read-owned-bots', bot_id)
            bot = (await self.owned()).get(bot_id)
            if bot is None:
                raise AccessDenied('bot_not_owned')
            info = await self.client(functions.bots.GetBotInfoRequest(
                bot=utils.get_input_user(bot), lang_code=''), flood_sleep_threshold=0)
            self.policy(agent, 'read-owned-bots', bot_id)
            value = {key: getattr(info, key, '') or '' for key in PROFILE_LIMITS}
            if not all(isinstance(v, str) for v in value.values()):
                raise RuntimeError('invalid_owned_bot_profile')
            return redact_tokens({'bot_id': bot_id, **value, 'owned': True, 'untrusted_content': True})

    async def edit(self, agent, bot_id, request_id, changes):
        valid_request_id(request_id)
        value = validate_profile(changes)
        self.policy(agent, 'edit-owned-bot-profile', bot_id, write=True)
        digest = fingerprint('bot-profile', bot_id=bot_id, changes=value)
        async with self.lock:
            self.policy(agent, 'edit-owned-bot-profile', bot_id, write=True)
            previous = self.receipts.existing(agent, request_id, digest)
            if previous is not None:
                return {k: v for k, v in previous.items() if k != 'fingerprint'}
            bots = await self.owned()
            bot = bots.get(bot_id)
            if bot is None:
                raise AccessDenied('bot_not_owned')
            peer = utils.get_input_user(bot)
            self.policy(agent, 'edit-owned-bot-profile', bot_id, write=True)
            path, previous = self.receipts.begin(agent, request_id, digest, bot_id, 'bot-profile')
            if previous is not None:
                return {k: v for k, v in previous.items() if k != 'fingerprint'}
            receipt = {'request_id': request_id, 'bot_id': bot_id, 'operation': 'bot-profile',
                       'status': 'uncertain_do_not_retry', 'readback_verified': False}
            try:
                # Authorization is rechecked after the durable claim. Telegram
                # itself enforces bot ownership again for the mutation.
                self.policy(agent, 'edit-owned-bot-profile', bot_id, write=True)
                accepted = await self.client(functions.bots.SetBotInfoRequest(
                    bot=peer, lang_code='', **value), flood_sleep_threshold=0)
                if accepted is True:
                    self.policy(agent, 'edit-owned-bot-profile', bot_id, write=True)
                    actual = await self.client(functions.bots.GetBotInfoRequest(
                        bot=peer, lang_code=''), flood_sleep_threshold=0)
                    if all(getattr(actual, key, None) == expected for key, expected in value.items()):
                        receipt.update(status='delivered_verified', readback_verified=True)
            except errors.FloodWaitError as exc:
                receipt['retry_after_seconds'] = exc.seconds
            except Exception:
                # Could have been applied; do not expose provider text or retry.
                pass
            self.receipts.finish(path, digest, receipt)
            self.receipts.clear_guard(agent, digest, receipt)
            return receipt


def register_owned_bots(runtime):
    """Register internal operations; the permission snapshot hides them by default."""
    from typing import Optional
    from pydantic import StrictInt
    from .agents import AgentRegistry
    from .config import private_dir

    services = {}

    def service(account):
        client = runtime.get_client(account)
        key = id(client)
        if key not in services:
            services[key] = OwnedBots(client, AgentRegistry(private_dir() / 'agents.json'),
                                      private_dir() / 'bot-profile-receipts')
        return services[key]

    async def safe(coro):
        try:
            return await coro
        except errors.FloodWaitError as exc:
            return {'error': 'telegram_cooldown', 'retry_after_seconds': exc.seconds}
        except AccessDenied:
            return {'error': 'bot_access_denied'}
        except (ValueError, TypeError):
            return {'error': 'invalid_bot_request'}
        except Exception:
            return {'error': 'bot_operation_failed'}

    @runtime.mcp.tool(annotations=runtime.ToolAnnotations(title='List owned bots', readOnlyHint=True, openWorldHint=True))
    @runtime.with_account(readonly=True)
    async def agent_list_owned_bots(agent_id: str, account: Optional[str] = None) -> dict:
        return await safe(service(account).list(agent_id))

    @runtime.mcp.tool(annotations=runtime.ToolAnnotations(title='Read owned bot profile', readOnlyHint=True, openWorldHint=True))
    @runtime.with_account(readonly=True)
    async def agent_get_owned_bot_profile(agent_id: str, bot_id: StrictInt, account: Optional[str] = None) -> dict:
        return await safe(service(account).get(agent_id, bot_id))

    @runtime.mcp.tool(annotations=runtime.ToolAnnotations(title='Edit granted owned bot profile',
                           readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
    @runtime.with_account(readonly=False)
    async def agent_update_owned_bot_profile(agent_id: str, bot_id: StrictInt, request_id: str,
                name: Optional[str] = None, about: Optional[str] = None,
                description: Optional[str] = None, account: Optional[str] = None) -> dict:
        changes = {k: v for k, v in {'name': name, 'about': about, 'description': description}.items() if v is not None}
        return await safe(service(account).edit(agent_id, bot_id, request_id, changes))
