"""Explicit opt-in facade for owned-bot metadata and owner-verified profile edits."""
import time

from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from mcp.server.fastmcp import Context
from pydantic import StrictInt

from .agents import check_owned_bot
from .owned_bots import validate_profile
from .poll_common import valid_request_id
from .sensitive_peer import redact_tokens


class OwnedBotGateway:
    def __init__(self, api): self.api = api

    def policy(self, agent, action, bot_id=None, *, write=False):
        check_owned_bot(self.api.registry.profile(agent), action, bot_id, write=write)

    async def call(self, agent, action, name, args, bot_id=None, *, write=False):
        self.policy(agent, action, bot_id, write=write)
        async with self.api.lock:
            self.policy(agent, action, bot_id, write=write)
            available = getattr(self.api.upstream, 'available_tools', None)
            if available is not None and name not in available:
                return {'error': 'bot_backend_not_enabled'}
            remaining = self.api.retry_at - time.monotonic()
            if remaining > 0:
                return {'error': 'telegram_cooldown', 'retry_after_seconds': int(remaining) + 1}
            result = await self.api.upstream.call(name, {'agent_id': agent, **args})
            self.policy(agent, action, bot_id, write=write)
            if not isinstance(result, dict):
                return {'error': 'bot_operation_failed'}
            if result.get('error'):
                wait = result.get('retry_after_seconds')
                if type(wait) is int and wait > 0:
                    self.api.retry_at = time.monotonic() + wait
                    return {'error': 'telegram_cooldown', 'retry_after_seconds': wait}
                return {'error': 'bot_operation_failed'}
            return redact_tokens(result)

    async def list(self, agent):
        result = await self.call(agent, 'read-owned-bots', 'agent_list_owned_bots', {})
        if result.get('error'): return result
        rows = result.get('bots')
        if (not isinstance(rows, list) or len(rows) > 1000 or any(
                not isinstance(r, dict) or type(r.get('bot_id')) is not int or r['bot_id'] <= 0
                or r.get('owned') is not True for r in rows)):
            raise ToolError('invalid_owned_bot_list')
        return {'bots': [{k: r[k] for k in ('bot_id', 'username', 'name', 'owned') if k in r}
                         for r in rows], 'untrusted_content': True}

    async def get(self, agent, bot_id):
        result = await self.call(agent, 'read-owned-bots', 'agent_get_owned_bot_profile',
                                 {'bot_id': bot_id}, bot_id)
        if result.get('error'): return result
        if result.get('bot_id') != bot_id or result.get('owned') is not True:
            raise ToolError('bot_identity_mismatch')
        return {k: result[k] for k in ('bot_id', 'name', 'about', 'description', 'owned', 'untrusted_content') if k in result}

    async def edit(self, agent, bot_id, request_id, name=None, about=None, description=None):
        valid_request_id(request_id)
        changes = validate_profile({k: v for k, v in {'name': name, 'about': about, 'description': description}.items()
                                    if v is not None})
        result = await self.call(agent, 'edit-owned-bot-profile', 'agent_update_owned_bot_profile',
                                 {'bot_id': bot_id, 'request_id': request_id, **changes}, bot_id, write=True)
        if result.get('error'): return result
        if (result.get('bot_id') != bot_id or result.get('request_id') != request_id
                or result.get('status') not in ('delivered_verified', 'uncertain_do_not_retry')):
            raise ToolError('bot_edit_receipt_invalid')
        return {k: result[k] for k in ('bot_id', 'request_id', 'operation', 'status', 'readback_verified',
                                       'retry_after_seconds', 'original_request_id') if k in result}


def register_owned_bot_gateway(mcp, service):
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)

    @mcp.tool(annotations=read)
    async def list_owned_bots(ctx: Context) -> dict:
        """List only bots Telegram confirms belong to this account, without tokens or chat history."""
        api, agent = service(ctx)
        return await OwnedBotGateway(api).list(agent)

    @mcp.tool(annotations=read)
    async def get_owned_bot_profile(ctx: Context, bot_id: StrictInt) -> dict:
        """Read name/about/description after live ownership verification; no credentials."""
        api, agent = service(ctx)
        return await OwnedBotGateway(api).get(agent, bot_id)

    @mcp.tool(annotations=write)
    async def update_owned_bot_profile(ctx: Context, bot_id: StrictInt, request_id: str,
                 name: str | None = None, about: str | None = None, description: str | None = None) -> dict:
        """Change an exact-granted bot or any currently owned bot under all-owned scope.

        Telegram ownership is checked before mutation even with all-owned scope.
        A unique request_id is mandatory. Never retry uncertain_do_not_retry
        with another ID. Does not create bots, fetch tokens, or change settings.
        """
        api, agent = service(ctx)
        return await OwnedBotGateway(api).edit(agent, bot_id, request_id, name, about, description)
