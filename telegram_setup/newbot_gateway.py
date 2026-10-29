"""Explicit fixed-input facade; no general BotFather messaging."""
import re
import time
from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations

from .agents import check_action
from .newbot_protocol import newbot_plan


class NewBotGateway:
    def __init__(self, api): self.api = api
    def policy(self, agent): check_action(self.api.registry.profile(agent), 'create-owned-bot')

    async def request(self, agent, name, username, request_id):
        newbot_plan(name, username, request_id); self.policy(agent)
        async with self.api.lock:
            self.policy(agent)
            available = getattr(self.api.upstream, 'available_tools', None)
            if available is not None and 'agent_request_owned_bot' not in available:
                return {'error': 'newbot_backend_not_enabled'}
            remaining = self.api.retry_at - time.monotonic()
            if remaining > 0:
                return {'error': 'telegram_cooldown', 'retry_after_seconds': int(remaining) + 1}
            try:
                result = await self.api.upstream.call('agent_request_owned_bot',
                    {'agent_id': agent, 'name': name, 'username': username, 'request_id': request_id})
                self.policy(agent)
            except Exception:
                return {'request_id': request_id, 'status': 'uncertain_do_not_retry', 'readback_verified': False}
            if not isinstance(result, dict):
                return {'request_id': request_id, 'status': 'uncertain_do_not_retry', 'readback_verified': False}
            if result.get('error'):
                return {'error': 'newbot_request_unavailable'}
            if result.get('request_id') != request_id:
                return {'request_id': request_id, 'status': 'uncertain_do_not_retry', 'readback_verified': False}
            if result.get('status') == 'created_verified':
                ref = result.get('token_ref')
                if (result.get('readback_verified') is not True or type(result.get('bot_id')) is not int
                        or result['bot_id'] <= 0 or result.get('username') != username
                        or result.get('authorization_mode') not in ('operator', 'delegated')
                        or result.get('owner_identity_verified') is not False
                        or not isinstance(ref, str) or not re.fullmatch(r'botcred_[0-9a-f]{64}', ref)):
                    return {'request_id': request_id, 'status': 'uncertain_do_not_retry', 'readback_verified': False}
                return {k: result[k] for k in ('request_id', 'status', 'readback_verified', 'bot_id', 'username',
                                               'token_ref', 'authorization_mode', 'owner_identity_verified') if k in result}
            safe = {'request_id': request_id, 'status': 'uncertain_do_not_retry', 'readback_verified': False}
            if result.get('authorization_mode') in ('operator', 'delegated'):
                safe.update(authorization_mode=result['authorization_mode'], owner_identity_verified=False)
            return safe


def register_newbot_gateway(mcp, service):
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
              idempotentHint=True, openWorldHint=True))
    async def request_owned_bot(ctx: Context, name: str, username: str, request_id: str) -> dict:
        """Create one exactly approved bot, or read the same durable receipt.

        Separate agent grant and configured operator or delegated mode are
        required. Delegated mode relies on the client to follow direct owner
        requests; the server cannot authenticate the human for each request.
        There is no model-supplied approval argument. Honour client confirmations.
        BotFather replies
        and tokens never enter tool output. uncertain_do_not_retry means inspect
        the server receipt, never repeat with another request ID.
        """
        api, agent = service(ctx)
        return await NewBotGateway(api).request(agent, name, username, request_id)
