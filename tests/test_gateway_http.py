"""Real Streamable HTTP over loopback; synthetic Telegram backend only."""
import asyncio
import contextlib
import json
import logging
import socket
import unittest

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from telegram_setup.client import text_payload
from telegram_setup.gateway import create_http_app
from test_agent_gateway import RegistryFixture, TOKEN_A, TOKEN_B


@contextlib.asynccontextmanager
async def client(url, token):
    async with streamablehttp_client(url, headers={'Authorization': 'Bearer ' + token}) as (rx, tx, _):
        async with ClientSession(rx, tx) as session:
            await session.initialize()
            yield session


class HttpTests(RegistryFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup_registry()
        self.connections = 0
        await self.start_http()

    async def start_http(self):

        @contextlib.asynccontextmanager
        async def fake_connection():
            self.connections += 1
            yield self.upstream

        app = create_http_app(self.registry, upstream_factory=fake_connection)
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0)); self.sock.listen()
        self.url = 'http://127.0.0.1:' + str(self.sock.getsockname()[1]) + '/mcp'
        self.server = uvicorn.Server(uvicorn.Config(app, log_level='critical', access_log=False, lifespan='on'))
        self.task = asyncio.create_task(self.server.serve(sockets=[self.sock]))
        for _ in range(100):
            if self.server.started: break
            if self.task.done(): await self.task
            await asyncio.sleep(0.02)
        self.assertTrue(self.server.started)

    async def asyncTearDown(self):
        self.server.should_exit = True
        await asyncio.wait_for(self.task, 5)
        self.sock.close()
        self.teardown_registry()

    async def test_two_clients_scope_reads_revocation_and_no_writes(self):
        async with client(self.url, TOKEN_B) as b:
            revoked = False
            try:
                async with client(self.url, TOKEN_A) as a:
                    self.assertEqual(self.connections, 1, 'One upstream connection is shared by both agents')
                    catalog = await a.list_tools()
                    self.assertEqual(len(catalog.tools), 6)
                    first = text_payload(await a.call_tool('read_history_page', {'chat_id': 11, 'limit': 2}))
                    second = text_payload(await b.call_tool('read_history_page', {'chat_id': 22, 'limit': 2}))
                    self.assertEqual(first['chat_id'], 11)
                    self.assertEqual(second['chat_id'], 22)
                    self.assertEqual([m['id'] for m in first['messages']], [1, 2])
                    before = len(self.upstream.calls)
                    self.assertTrue((await a.call_tool('read_history_page', {'chat_id': 22})).isError)
                    for name in ('send_message', 'send_file', 'edit_message', 'delete_message', 'mark_as_read'):
                        self.assertTrue((await a.call_tool(name, {'chat_id': 11})).isError)
                    self.assertEqual(len(self.upstream.calls), before)
                    self.data['agents']['a']['enabled'] = False; self.registry.save(self.data)
                    revoked = True
                    await a.call_tool('telegram_status', {})
                    self.fail('Revoked HTTP session was accepted')
            except BaseExceptionGroup as error:
                self.assertTrue(revoked)
                pending = list(error.exceptions)
                while pending:
                    leaf = pending.pop()
                    if isinstance(leaf, BaseExceptionGroup): pending.extend(leaf.exceptions)
                    else:
                        self.assertIsInstance(leaf, httpx.HTTPStatusError)
                        self.assertEqual(leaf.response.status_code, 401)
            result = text_payload(await b.call_tool('telegram_status', {}))
            self.assertTrue(result['connected'])
            self.assertEqual(self.connections, 1)

    async def test_unauthenticated_and_wrong_origin_are_rejected(self):
        async with httpx.AsyncClient() as client_http:
            r = await client_http.post(self.url, json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
            self.assertEqual(r.status_code, 401)
            r = await client_http.post(self.url, headers={'Authorization': 'Bearer ' + TOKEN_A,
                'Origin': 'https://untrusted.example.invalid'}, json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
            self.assertEqual(r.status_code, 403)
        self.assertEqual(self.upstream.calls, [])

    async def test_protocol_restart_resumes_from_checkpoint(self):
        async with client(self.url, TOKEN_A) as a:
            first = text_payload(await a.call_tool('read_history_page', {'chat_id': 11, 'limit': 2}))
        checkpoint = first['next_after_id']
        self.server.should_exit = True
        await asyncio.wait_for(self.task, 5); self.sock.close()
        await self.start_http()
        async with client(self.url, TOKEN_A) as a:
            second = text_payload(await a.call_tool('read_history_page',
                {'chat_id': 11, 'after_id': checkpoint, 'limit': 2}))
        self.assertEqual([m['id'] for m in second['messages']], [3, 4])
        self.assertEqual(self.connections, 2)


if __name__ == '__main__':
    unittest.main()
