"""Gateway for owner-only bot notifications; explicit bot and action grants."""
import time
from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations
from pydantic import StrictInt

from .agents import check_bot_notify
from .poll_common import valid_request_id
from .sensitive_peer import redact_tokens


class OwnerBotNotificationGateway:
    def __init__(self, api): self.api = api
    def policy(self, agent, bot_id): check_bot_notify(self.api.registry.profile(agent), bot_id)
    async def send(self, agent, bot_id, text, request_id):
        valid_request_id(request_id); self.policy(agent, bot_id)
        if (not isinstance(text, str) or not text.strip() or len(text.encode('utf-16-le')) // 2 > 4096
                or redact_tokens(text) != text): raise ValueError('invalid_notification_text')
        async with self.api.lock:
            self.policy(agent, bot_id)
            available = getattr(self.api.upstream, 'available_tools', None)
            if available is not None and 'agent_notify_owner_via_bot' not in available:
                return {'error': 'bot_notification_backend_not_enabled'}
            remaining = self.api.retry_at - time.monotonic()
            if remaining > 0:
                return {'error': 'telegram_cooldown', 'retry_after_seconds': int(remaining) + 1}
            try:
                result = await self.api.upstream.call('agent_notify_owner_via_bot',
                    {'agent_id': agent, 'bot_id': bot_id, 'text': text, 'request_id': request_id})
                self.policy(agent, bot_id)
            except Exception:
                return {'request_id': request_id, 'bot_id': bot_id, 'status': 'uncertain_do_not_retry',
                        'readback_verified': False}
            if not isinstance(result, dict) or result.get('request_id') != request_id or result.get('bot_id') != bot_id:
                # Denials are returned as fixed errors; no provider body escapes.
                if isinstance(result, dict) and result.get('error'):
                    return {'error': 'bot_notification_not_authorized'}
                return {'request_id': request_id, 'bot_id': bot_id, 'status': 'uncertain_do_not_retry',
                        'readback_verified': False}
            if result.get('status') not in ('delivered_verified', 'accepted_unconfirmed',
                                            'rejected_not_sent', 'uncertain_do_not_retry'):
                return {'request_id': request_id, 'bot_id': bot_id, 'status': 'uncertain_do_not_retry',
                        'readback_verified': False}
            if result['status'] == 'delivered_verified' and result.get('readback_verified') is not True:
                return {'request_id': request_id, 'bot_id': bot_id, 'status': 'uncertain_do_not_retry',
                        'readback_verified': False}
            return {k: result[k] for k in ('request_id', 'bot_id', 'operation', 'status', 'readback_verified',
                                           'message_id', 'error_code', 'retry_after_seconds', 'original_request_id') if k in result}


def register_owner_bot_notification_gateway(mcp, service):
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
              idempotentHint=True, openWorldHint=True))
    async def notify_owner_from_bot(ctx: Context, bot_id: StrictInt, text: str, request_id: str) -> dict:
        """Send one text to the account owner from one expressly permitted owned bot.

        The recipient is derived from the existing Telegram user session; no
        recipient, token, URL or account argument is accepted. The owner must
        already have started the bot. accepted_unconfirmed is not readback;
        never repeat uncertainty with a new request_id.
        """
        api, agent = service(ctx)
        return await OwnerBotNotificationGateway(api).send(agent, bot_id, text, request_id)
