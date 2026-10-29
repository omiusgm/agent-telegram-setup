"""Default-off internal tool for on-demand notifications to account owner."""
from typing import Optional
from pydantic import StrictInt
from .agents import AccessDenied, AgentRegistry
from .config import private_dir
from .owner_bot_notifications import OwnerBotNotifier


def register_owner_bot_notifications(runtime):
    services = {}
    def service(account):
        client = runtime.get_client(account); key = id(client)
        if key not in services:
            services[key] = OwnerBotNotifier(client, AgentRegistry(private_dir() / 'agents.json'), private_dir())
        return services[key]

    @runtime.mcp.tool(annotations=runtime.ToolAnnotations(title='Notify the account owner from an allowed bot',
                      readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
    @runtime.with_account(readonly=False)
    async def agent_notify_owner_via_bot(agent_id: str, bot_id: StrictInt, text: str, request_id: str,
                                         account: Optional[str] = None) -> dict:
        try:
            return await service(account).send(agent_id, bot_id, text, request_id)
        except AccessDenied:
            return {'error': 'bot_notification_not_authorized'}
        except Exception:
            return {'request_id': request_id, 'bot_id': bot_id, 'status': 'uncertain_do_not_retry',
                    'readback_verified': False}
