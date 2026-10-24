import asyncio
import json
import os
import socket
import stat
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from telegram_setup.config import ROOT, connection_lock
from telegram_setup.relay import socket_path
from telegram_setup.owned_inventory import _INVENTORIES
from telegram_setup.shared import serve_socket, should_keep_alive
from telegram_setup.permissions import save_settings


@asynccontextmanager
async def client(home):
    env = dict(os.environ, AGENT_TELEGRAM_HOME=home)
    env.pop('AGENT_TELEGRAM_ACCESS', None)
    parameters = StdioServerParameters(command=sys.executable,
        args=[str(ROOT / 'tests/offline_shared.py')], env=env, cwd=str(ROOT))
    with open(os.devnull, 'w') as stderr:
        async with stdio_client(parameters, errlog=stderr) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                yield session


async def identity(session):
    result = await session.call_tool('offline_backend_identity', {})
    if result.isError:
        raise AssertionError('Offline identity tool failed')
    return json.loads(next(c.text for c in result.content if c.type == 'text'))


class SharedTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_service_stays_alive_until_released(self):
        from mcp.server.fastmcp import FastMCP
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'mcp.sock'
            persistent = [True]
            task = asyncio.create_task(serve_socket(FastMCP('persistent-fixture'), path,
                idle_seconds=0.05, keep_alive=lambda: persistent[0]))
            try:
                await asyncio.sleep(0.2)
                self.assertTrue(path.exists())
                self.assertFalse(task.done())
                persistent[0] = False
                await asyncio.wait_for(task, 2)
                self.assertFalse(path.exists())
            finally:
                if not task.done():
                    task.cancel(); await asyncio.gather(task, return_exceptions=True)

    def test_managed_backend_never_spawns_a_competing_process(self):
        from telegram_setup import relay
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ,
                {'AGENT_TELEGRAM_HOME': home, 'AGENT_TELEGRAM_MANAGED_BACKEND': '1'}):
            path = socket_path()
            try:
                with patch.object(relay, 'connect', return_value=None), patch.object(relay.subprocess, 'Popen') as spawn:
                    with self.assertRaises(RuntimeError): relay.open_connection()
                    spawn.assert_not_called()
            finally:
                path.parent.rmdir()

    async def test_pending_inventory_survives_idle_between_clients(self):
        from mcp.server.fastmcp import FastMCP
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
                os.environ, {'AGENT_TELEGRAM_HOME': temporary}, clear=True), patch.dict(
                _INVENTORIES, {'synthetic': {'dialogs_complete': False}}, clear=True):
            settings = save_settings({'schema_version': 1, 'enabled': ['read'], 'transcription_engine': None})
            path = Path(temporary) / 'mcp.sock'
            task = asyncio.create_task(serve_socket(FastMCP('synthetic-pending-work'), path,
                idle_seconds=0.05, keep_alive=lambda: should_keep_alive(settings)))
            try:
                await asyncio.sleep(0.2)
                self.assertTrue(path.exists())
                self.assertFalse(task.done(), 'Idle exit would discard the partial cursor/FloodWait')
                save_settings({'schema_version': 1, 'enabled': ['read', 'download'], 'transcription_engine': None})
                await asyncio.wait_for(task, timeout=2)
                self.assertFalse(path.exists())
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_two_clients_one_account_process_disconnect_and_idle_release(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'AGENT_TELEGRAM_HOME': home}):
            path = socket_path()
            async with client(home) as first:
                async with client(home) as second:
                    a, b = await asyncio.gather(identity(first), identity(second))
                    self.assertEqual(a, b)
                    self.assertTrue(a['synthetic'])
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                    with self.assertRaises(RuntimeError):
                        with connection_lock():
                            self.fail('The backend must keep the account lock')
                await asyncio.sleep(0.8)
                self.assertEqual(await identity(first), a)
                # Bad local protocol input cannot take down the other session.
                bad = socket.socket(socket.AF_UNIX)
                bad.connect(str(path))
                bad.sendall(b'invalid JSON\n')
                bad.close()
                await asyncio.sleep(0.1)
                self.assertEqual(await identity(first), a)
            for _ in range(40):
                if not path.exists():
                    break
                await asyncio.sleep(0.1)
            self.assertFalse(path.exists(), 'Idle backend did not release its socket')
            with connection_lock():
                pass
            path.parent.rmdir()

    def test_short_socket_paths_and_separate_installations(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {'AGENT_TELEGRAM_HOME': temporary + '/deep' * 40}):
                first = socket_path()
                self.assertLess(len(str(first).encode()), 104)
                self.assertEqual(stat.S_IMODE(first.parent.stat().st_mode), 0o700)
            with patch.dict(os.environ, {'AGENT_TELEGRAM_HOME': temporary + '/another'}):
                second = socket_path()
                self.assertNotEqual(first, second)
            first.parent.rmdir()
            second.parent.rmdir()

    def test_rejects_unsafe_socket_directory(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'AGENT_TELEGRAM_HOME': home}):
            path = socket_path()
            path.parent.chmod(0o755)
            try:
                with self.assertRaises(RuntimeError):
                    socket_path()
            finally:
                path.parent.chmod(0o700)
                path.parent.rmdir()


if __name__ == '__main__':
    unittest.main()
