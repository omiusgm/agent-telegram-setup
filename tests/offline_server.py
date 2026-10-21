"""Synthetic session only; any Internet socket connection is a test failure."""

import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from telegram_setup.config import connection_lock, save_credentials
from telegram_setup.server import prepare

original_connect = socket.socket.connect


def no_internet(self, address):
    if self.family in {socket.AF_INET, socket.AF_INET6}:
        raise AssertionError("Offline test attempted a network connection")
    return original_connect(self, address)


socket.socket.connect = no_internet
save_credentials({"api_id": 12345, "api_hash": "0" * 32, "authorized": True, "user_id": 1000})
with connection_lock():
    runner = prepare()

    async def offline():
        await runner._serve("stdio")

    runner._main = offline
    runner.main()

