"""Stdio facade with fake Telegram; the socket guard forbids the Internet."""
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
original_connect = socket.socket.connect
def no_internet(self, address):
    if self.family in {socket.AF_INET, socket.AF_INET6}:
        raise AssertionError('offline fixture attempted Internet')
    return original_connect(self, address)
socket.socket.connect = no_internet

from telegram_setup.agents import AgentRegistry
from telegram_setup.gateway import create_gateway
from telegram_setup.owned_bots import OwnedBots
from test_owned_bots import BotClient

registry = AgentRegistry(sys.argv[1])
backend = OwnedBots(BotClient(), registry, Path(sys.argv[2]))

class FakeBridge:
    available_tools = {'get_me', 'agent_list_owned_bots', 'agent_get_owned_bot_profile', 'agent_update_owned_bot_profile'}
    async def call(self, name, args):
        if name == 'get_me': return {'type': 'user', 'id': 1001}
        if name == 'agent_list_owned_bots': return await backend.list(args['agent_id'])
        if name == 'agent_get_owned_bot_profile': return await backend.get(args['agent_id'], args['bot_id'])
        if name == 'agent_update_owned_bot_profile':
            fields = {k: args[k] for k in ('name', 'about', 'description') if k in args}
            return await backend.edit(args['agent_id'], args['bot_id'], args['request_id'], fields)
        raise AssertionError('unexpected operation')

@asynccontextmanager
async def upstream(): yield FakeBridge()

create_gateway(registry, 'a', upstream, enable_owned_bots=True).run(transport='stdio')
