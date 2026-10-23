"""Synthetic shared backend and real stdio relay; Internet is prohibited."""

import os
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
original_connect = socket.socket.connect


def no_internet(self, address):
    if self.family in {socket.AF_INET, socket.AF_INET6}:
        raise AssertionError('Offline test attempted an Internet connection')
    return original_connect(self, address)


socket.socket.connect = no_internet

if '--backend' in sys.argv:
    from mcp.types import ToolAnnotations
    from telegram_setup.config import connection_lock, save_credentials
    from telegram_setup.permissions import save_settings
    from telegram_setup.relay import socket_path
    from telegram_setup.server import prepare
    from telegram_setup.shared import serve_socket, should_keep_alive

    save_settings({'schema_version': 1, 'enabled': ['read'], 'transcription_engine': None})
    save_credentials({'api_id': 12345, 'api_hash': '0' * 32, 'authorized': True, 'user_id': 1000})
    with connection_lock():
        runner = prepare()

        @runner.mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
        def offline_backend_identity() -> dict:
            return {'pid': os.getpid(), 'synthetic': True}

        async def offline():
            await serve_socket(runner.mcp, socket_path(), idle_seconds=0.5,
                keep_alive=lambda: should_keep_alive(runner._telegram_setup_permissions))

        runner._main = offline
        runner.main()
else:
    from telegram_setup import relay
    original_popen = subprocess.Popen

    def fake_backend(_args, **kwargs):
        return original_popen([sys.executable, str(Path(__file__).resolve()), '--backend'], **kwargs)

    relay.subprocess.Popen = fake_backend
    sys.exit(relay.main())
