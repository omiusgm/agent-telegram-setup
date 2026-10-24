"""Synthetic stdio reader; prohibits Internet and never opens Telegram auth."""
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

original_connect = socket.socket.connect
def no_internet(self, address):
    if self.family in {socket.AF_INET, socket.AF_INET6}:
        raise AssertionError('Offline reader attempted Internet')
    return original_connect(self, address)
socket.socket.connect = no_internet

from telegram_setup.agents import AgentRegistry
from telegram_setup.gateway import create_gateway
from test_agent_gateway import FakeUpstream

@asynccontextmanager
async def upstream():
    yield FakeUpstream()

create_gateway(AgentRegistry(sys.argv[1]), sys.argv[2], upstream).run(transport='stdio')
