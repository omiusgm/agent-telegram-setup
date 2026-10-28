"""Exercise native MCP metadata, tools, embedded binary and registered resources."""
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from telegram_setup.client import text_payload
from telegram_setup.config import ROOT
from test_agent_gateway import RegistryFixture


class SearchFilesStdioTests(RegistryFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self): self.setup_registry()
    def tearDown(self): self.teardown_registry()

    async def test_real_stdio_binary_output_and_resource_read_exact_bytes_not_cloud_artifact_claim(self):
        self.data['agents']['a']['all_chats'] = True
        self.data['agents']['a']['actions'] += ['send-saved-file', 'download-saved-file', 'public-search']
        self.registry.save(self.data)
        params = StdioServerParameters(command=sys.executable, args=[str(ROOT / 'tests/offline_search_files.py'),
                 str(self.registry.path), str(Path(self.temporary.name) / 'state')],
                 env={'AGENT_TELEGRAM_HOME': str(Path(self.temporary.name) / 'home')})
        with open(os.devnull, 'w') as stderr:
            async with stdio_client(params, errlog=stderr) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    tools = {t.name: t for t in (await session.list_tools()).tools}
                    self.assertEqual(len(tools), 14)
                    self.assertEqual(tools['stage_file'].meta['openai/fileParams'], ['file'])
                    self.assertIn('resource_uri', tools['download_saved_file'].outputSchema['properties'])
                    searched = text_payload(await session.call_tool('search_allowed_messages', {'query': 'fixture'}))
                    self.assertTrue(searched['complete'])
                    text = 'Synthetic stdio fixture. No personal data.\n'
                    staged = text_payload(await session.call_tool('stage_text_file', {'text': text, 'file_name': 'fixture.txt'}))
                    self.assertFalse(staged['native_host_input_verified'])
                    self.assertEqual(staged['sha256'], hashlib.sha256(text.encode()).hexdigest())
                    sent = text_payload(await session.call_tool('send_saved_file',
                        {'staging_id': staged['staging_id'], 'request_id': 'stdio-synthetic-file-1'}))
                    self.assertEqual(sent['status'], 'delivered_verified')
                    result = await session.call_tool('download_saved_file', {'message_id': 501})
                    self.assertFalse(result.isError)
                    self.assertFalse(result.structuredContent['dot_file_received_verified'])
                    binary = next(c.resource for c in result.content if c.type == 'resource')
                    self.assertEqual(base64.b64decode(binary.blob), text.encode())
                    read = await session.read_resource(result.structuredContent['resource_uri'])
                    self.assertEqual(base64.b64decode(read.contents[0].blob), text.encode())
                    self.assertTrue((await session.list_resource_templates()).resourceTemplates)
                    self.data['agents']['a']['actions'].remove('download-saved-file'); self.registry.save(self.data)
                    with self.assertRaises(Exception):
                        await session.read_resource(result.structuredContent['resource_uri'])
                    denied = await session.call_tool('get_staged_file_resource',
                                                     {'staging_id': sent['download_staging_id']})
                    self.assertTrue(denied.isError)

    async def test_existing_reader_identity_cannot_use_new_tools_even_when_catalog_enabled(self):
        params = StdioServerParameters(command=sys.executable, args=[str(ROOT / 'tests/offline_search_files.py'),
                 str(self.registry.path), str(Path(self.temporary.name) / 'state')],
                 env={'AGENT_TELEGRAM_HOME': str(Path(self.temporary.name) / 'home')})
        with open(os.devnull, 'w') as stderr:
            async with stdio_client(params, errlog=stderr) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    for name, args in [('stage_text_file', {'text': 'test'}),
                                       ('download_saved_file', {'message_id': 501}),
                                       ('search_public_communities', {'query': 'fixture'})]:
                        self.assertTrue((await session.call_tool(name, args)).isError)
                    result = text_payload(await session.call_tool('search_allowed_messages', {'query': 'fixture'}))
                    self.assertTrue(all(r['chat_id'] == 11 for r in result['messages']))
