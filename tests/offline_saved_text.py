"""Synthetic stdio write MVP. Internet and Telegram authorization are forbidden."""
from contextlib import asynccontextmanager
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
original_connect = socket.socket.connect


def offline_connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        raise AssertionError('Offline Saved Messages test attempted Internet')
    return original_connect(self, address)


socket.socket.connect = offline_connect
from telegram_setup.agents import AgentRegistry
from telegram_setup.gateway import create_gateway
from test_saved_text import FakeSavedUpstream


@asynccontextmanager
async def upstream():
    yield FakeSavedUpstream()


create_gateway(AgentRegistry(sys.argv[1]), 'a', upstream,
               enable_saved_text=True, receipt_dir=sys.argv[2]).run(transport='stdio')
