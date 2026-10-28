import base64
import contextlib
import hashlib
import unittest

from telegram_setup.file_probe import FIXTURE_BYTES, FIXTURE_URI, probe_result
from telegram_setup.gateway import create_gateway
from test_agent_gateway import RegistryFixture


class ProbeTests(RegistryFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self): self.setup_registry()
    def tearDown(self): self.teardown_registry()

    async def test_fixed_bytes_no_telegram_calls_or_grants_needed(self):
        reply = probe_result()
        self.assertEqual(base64.b64decode(reply.content[1].resource.blob), FIXTURE_BYTES)
        self.assertEqual(reply.structuredContent['sha256'], hashlib.sha256(FIXTURE_BYTES).hexdigest())
        @contextlib.asynccontextmanager
        async def upstream(): yield self.upstream
        server = create_gateway(self.registry, 'a', upstream, enable_file_probe=True)
        tool = next(t for t in await server.list_tools() if t.name == 'file_transport_probe')
        self.assertTrue(tool.annotations.readOnlyHint)
        self.assertEqual(tool.inputSchema['properties'], {})
        self.assertIn('sha256', tool.outputSchema['properties'])
        async with server._mcp_server.lifespan(server._mcp_server):
            result = await server._tool_manager.call_tool('file_transport_probe', {})
            resource = list(await server.read_resource(FIXTURE_URI))
            self.assertEqual(resource[0].content, FIXTURE_BYTES)
        self.assertEqual(result.structuredContent['telegram_calls'], 0)
        self.assertEqual(self.upstream.calls, [])

    async def test_probe_requires_fixed_owner_bound_stdio_profile(self):
        with self.assertRaisesRegex(ValueError, 'fixed_stdio'):
            create_gateway(self.registry, enable_file_probe=True)
