"""Real stdio notification gateway with synthetic Telegram and HTTP."""
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
original = socket.socket.connect
def no_internet(self, address):
    if self.family in {socket.AF_INET, socket.AF_INET6}: raise AssertionError('internet forbidden')
    return original(self, address)
socket.socket.connect = no_internet

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from telegram_setup.agents import AgentRegistry
from telegram_setup.gateway import create_gateway
import telegram_setup.owner_bot_notification_backend as backend_module
from telegram_setup.owner_bot_notifications import OwnerBotNotifier
from test_owner_bot_notifications import Client, BOT_MESSAGE_ID, BOT_DATE
import telegram_setup.owner_bot_notifications as notifications_module
notifications_module._HISTORY_PAUSE=0

root, registry_path = Path(sys.argv[1]), Path(sys.argv[2])
client = Client(); backend = FastMCP('synthetic backend')
backend_module.private_dir = lambda: root
backend_module.OwnerBotNotifier = lambda client, registry, folder: OwnerBotNotifier(
    client, registry, folder, transport=lambda *_: {'status':'accepted_by_telegram',
        'bot_message_id':BOT_MESSAGE_ID,'bot_date_unix':BOT_DATE})
runtime = SimpleNamespace(mcp=backend, ToolAnnotations=ToolAnnotations,
                          with_account=lambda **_: lambda fn: fn, get_client=lambda _: client)
backend_module.register_owner_bot_notifications(runtime)

class Bridge:
    available_tools = {'agent_notify_owner_via_bot', 'get_me'}
    async def call(self, name, args):
        if name == 'get_me': return {'type': 'user', 'id': 101}
        return await backend._tool_manager.call_tool(name, args)

@asynccontextmanager
async def upstream(): yield Bridge()

create_gateway(AgentRegistry(registry_path), 'a', upstream, enable_bot_notifications=True).run(transport='stdio')
