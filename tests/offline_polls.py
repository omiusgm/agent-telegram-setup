"""Synthetic stdio gateway: no Internet, no Telegram credentials or session."""
from contextlib import asynccontextmanager
from pathlib import Path
import socket
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
original=socket.socket.connect
def offline_connect(self,address):
    if self.family in (socket.AF_INET,socket.AF_INET6):
        raise AssertionError('offline poll test attempted Internet')
    return original(self,address)
socket.socket.connect=offline_connect

from telegram_setup.agents import AgentRegistry
from telegram_setup.gateway import create_gateway
from test_polls import FakeBackend

@asynccontextmanager
async def upstream(): yield FakeBackend()

create_gateway(AgentRegistry(sys.argv[1]),'a',upstream,enable_polls=True,
               receipt_dir=sys.argv[2]).run(transport='stdio')
