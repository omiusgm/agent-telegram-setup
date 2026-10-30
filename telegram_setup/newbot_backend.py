"""Private backend wiring for the fixed new-bot worker, default disabled."""
from functools import wraps
import logging
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp.exceptions import ToolError

from .agents import AccessDenied, AgentRegistry, check_action
from .config import private_dir
from .newbot_worker import NewBotWorker, logging_is_quiet


def telethon_logging_summary():
    """Fixed metadata only: never emit logger names, messages or credentials."""
    names = ['telethon'] + [n for n in logging.root.manager.loggerDict if n.startswith('telethon.')]
    return {'quiet': all(logging.getLogger(n).getEffectiveLevel() >= logging.WARNING for n in names),
            'below_warning_count': sum(logging.getLogger(n).getEffectiveLevel() < logging.WARNING for n in names)}


def enforce_telethon_warning():
    """Only raise unsafe Telethon logging levels; never weaken a stricter level."""
    names = ['telethon'] + [n for n in logging.root.manager.loggerDict if n.startswith('telethon.')]
    changed = 0
    for name in names:
        logger = logging.getLogger(name)
        if logger.getEffectiveLevel() < logging.WARNING:
            logger.setLevel(logging.WARNING); changed += 1
    return changed


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


def register_newbot_backend(runtime, *, creation_enabled=False):
    if type(creation_enabled) is not bool: raise ValueError('invalid_newbot_enabled_state')
    before = telethon_logging_summary()
    changed = enforce_telethon_warning() if creation_enabled else 0
    after = telethon_logging_summary()
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

    @runtime.mcp.tool(annotations=runtime.ToolAnnotations(title='Inspect bot creation readiness',
                      readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    @runtime.with_account(readonly=False)
    async def agent_newbot_preflight(agent_id: str, account: Optional[str] = None) -> dict:
        """Fixed diagnostic only: no BotFather request, claims, tokens or sends."""
        try:
            profile = AgentRegistry(private_dir() / 'agents.json').profile(agent_id)
            check_action(profile, 'create-owned-bot')
            if runtime.get_client(account) is None: raise RuntimeError('existing_account_unavailable')
            current = telethon_logging_summary()
            return {'policy_allowed': True, 'creation_enabled': creation_enabled,
                    'creation_mode': profile.get('bot_create_mode', 'operator'),
                    'startup_logging_before': before, 'startup_logging_after': after,
                    'startup_levels_raised': changed, 'current_logging': current,
                    'current_guard_quiet': logging_is_quiet(),
                    'barrier_running': barrier.running, 'barrier_active': barrier.active,
                    'barrier_ready_for_one': not barrier.active and barrier.running == 1,
                    'botfather_calls': 0, 'creation_attempted': False}
        except AccessDenied:
            return {'error': 'newbot_preflight_not_authorized'}
        except Exception:
            return {'error': 'newbot_preflight_unavailable'}
