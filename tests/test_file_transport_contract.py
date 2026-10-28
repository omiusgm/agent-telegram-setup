"""Real MCP pipes and client-side reconstruction; never a live Dot/file claim."""
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from telegram_setup.client import text_payload
from telegram_setup.config import ROOT


@contextlib.asynccontextmanager
async def probe():
    params = StdioServerParameters(command=sys.executable,
        args=[str(ROOT / 'tests/offline_file_transport.py')], cwd=str(ROOT))
    with open(os.devnull, 'w') as log:
        async with stdio_client(params, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                yield session


def payload(data, offset, full, request_id='file-probe-001'):
    return {'request_id': request_id, 'offset': offset, 'total_size': len(full),
            'file_sha256': hashlib.sha256(full).hexdigest(),
            'chunk_sha256': hashlib.sha256(data).hexdigest(),
            'data_b64': base64.b64encode(data).decode()}


class FileTransportContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_params_descriptor_declares_four_properties_required_two(self):
        async with probe() as session:
            tools = (await session.list_tools()).tools
            tool = next(t for t in tools if t.name == 'probe_file_reference')
            self.assertEqual(tool.model_dump(by_alias=True)['_meta']['openai/fileParams'], ['file'])
            model = tool.inputSchema['$defs']['OpenAIFile']
            self.assertEqual(set(model['properties']), {'download_url','file_id','mime_type','file_name'})
            self.assertEqual(set(model['required']), {'download_url','file_id'})
            self.assertTrue(all(value['type'] == 'string' for value in model['properties'].values()))
            result = text_payload(await session.call_tool('probe_file_reference', {'file': {
                'download_url': 'https://files.example.invalid/probe.bin', 'file_id': 'file_fixture_demo'}}))
            self.assertTrue(result['schema_accepted'])
            self.assertFalse(result['native_host_injection_verified'])
            self.assertFalse(result['file_bytes_fetched'])

    async def test_binary_roundtrip_replays_offsets_and_reconstructs_a_client_file(self):
        original = bytes(range(256)) * 129 + 'Тест байтов'.encode()
        expected = hashlib.sha256(original).hexdigest()
        async with probe() as session:
            for offset in range(0,len(original),16384):
                piece = original[offset:offset+16384]
                args = {'payload': payload(piece, offset, original)}
                result = await session.call_tool('probe_write_chunk', args)
                self.assertFalse(result.isError)
                replay = text_payload(await session.call_tool('probe_write_chunk', args))
                self.assertTrue(replay['replayed'])
            received = bytearray(); offset = 0
            while offset < len(original):
                result = await session.call_tool('probe_read_chunk', {'offset': offset, 'limit': 16384})
                self.assertFalse(result.isError)
                value = text_payload(result)
                piece = base64.b64decode(value['data_b64'], validate=True)
                self.assertEqual(hashlib.sha256(piece).hexdigest(), value['chunk_sha256'])
                self.assertEqual(value['file_sha256'], expected)
                self.assertFalse(value['cloud_file_artifact_created'])
                received.extend(piece); offset = value['next_offset']
            with tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary)/'client-side-probe.bin'; output.write_bytes(received)
                self.assertEqual(output.stat().st_size,len(original))
                self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(),expected)
                self.assertEqual(output.read_bytes(),original)
            self.assertTrue(value['eof'])

    async def test_bad_hash_size_offset_and_linux_path_are_rejected(self):
        async with probe() as session:
            good = payload(b'abc', 0, b'abc')
            cases = [dict(good, chunk_sha256='not-a-digest'), dict(good, offset=1),
                     payload(b'a'*16385,0,b'a'*16385), dict(good,file_path='outside-fixture')]
            for value in cases:
                self.assertTrue((await session.call_tool('probe_write_chunk', {'payload':value})).isError)
            self.assertFalse((await session.call_tool('probe_write_chunk', {'payload':good})).isError)
            changed = payload(b'xyz',0,b'xyz')
            self.assertTrue((await session.call_tool('probe_write_chunk', {'payload':changed})).isError)

    def test_maximum_chunk_fits_current_http_and_tunnel_json_budgets(self):
        raw = b'x'*16384
        request = {'jsonrpc':'2.0','id':1,'method':'tools/call','params':{
            'name':'probe_write_chunk','arguments':{'payload':payload(raw,0,raw)}}}
        size = len(json.dumps(request).encode())
        self.assertLess(size+4096,65536)
        self.assertLess(size+4096,4*1024**2)
        self.assertLess(size+4096,10*1024**2)


if __name__ == '__main__':
    unittest.main()
