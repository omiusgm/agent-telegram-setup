import os
import sys
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from telegram_setup.client import text_payload
from telegram_setup.config import ROOT
from test_agent_gateway import RegistryFixture


class StdioGatewayTests(RegistryFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self): self.setup_registry()
    def tearDown(self): self.teardown_registry()

    async def test_fixed_agent_tunnel_profile_and_live_revocation(self):
        params = StdioServerParameters(command=sys.executable,
            args=[str(ROOT/'tests/offline_gateway.py'), str(self.registry.path), 'a'], cwd=str(ROOT))
        with open(os.devnull, 'w') as errors:
            async with stdio_client(params, errlog=errors) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()
                    self.assertEqual(len((await client.list_tools()).tools), 6)
                    result = text_payload(await client.call_tool('read_history_page', {'chat_id': 11}))
                    self.assertEqual(result['chat_id'], 11)
                    self.assertTrue((await client.call_tool('read_history_page', {'chat_id': 22, 'agent': 'b'})).isError)
                    self.data['agents']['a']['enabled'] = False; self.registry.save(self.data)
                    self.assertTrue((await client.call_tool('telegram_status', {})).isError)


if __name__ == '__main__': unittest.main()
