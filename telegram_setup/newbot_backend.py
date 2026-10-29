"""Private backend wiring for the fixed new-bot worker, default disabled."""
from functools import wraps
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp.exceptions import ToolError

from .agents import AgentRegistry
from .config import private_dir
from .newbot_worker import NewBotWorker


class SharedCallBarrier:
    """Quiesce shared MCP calls during the short secret-bearing exchange."""
    def __init__(self): self.running = 0; self.active = False

    def install(self, mcp):
        original = mcp._tool_manager.call_tool
        @wraps(original)
        async def call(*args, **kwargs):
            if self.active: raise ToolError('newbot_exchange_in_progress')
            self.running += 1
            try: return await original(*args, **kwargs)
            finally: self.running -= 1
        mcp._tool_manager.call_tool = call

    def enter(self):
        # The one active call is the create request itself.
        if self.active or self.running != 1: raise PermissionError('shared_backend_busy')
        self.active = True

    def exit(self): self.active = False


def register_newbot_backend(runtime):
    barrier = SharedCallBarrier(); barrier.install(runtime.mcp)
    services = {}

    def worker(account):
        client = runtime.get_client(account); key = id(client)
        if key not in services:
            services[key] = NewBotWorker(client, AgentRegistry(private_dir() / 'agents.json'),
                                        private_dir(), barrier=barrier)
        return services[key]

    @runtime.mcp.tool(annotations=runtime.ToolAnnotations(title='Request one explicitly approved owned bot',
                      readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
    @runtime.with_account(readonly=False)
    async def agent_request_owned_bot(agent_id: str, name: str, username: str, request_id: str,
                                      account: Optional[str] = None) -> dict:
        try:
            return await worker(account).create(agent_id, name, username, request_id)
        except Exception:
            # Pre-dispatch failure: never include raw SDK/provider exceptions.
            return {'error': 'newbot_request_unavailable'}
