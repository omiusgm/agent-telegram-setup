import base64
import hashlib
import os
from pathlib import Path
import sys
import unittest
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from telegram_setup.config import ROOT
from test_agent_gateway import RegistryFixture


class AttachmentStdioTests(RegistryFixture,unittest.IsolatedAsyncioTestCase):
    def setUp(self):self.setup_registry()
    def tearDown(self):self.teardown_registry()
    async def test_actual_large_binary_mcp_result_and_chunk_reconstruction(self):
        self.data['agents']['a']['actions'].append('read-attachments');self.registry.save(self.data)
        params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'tests/offline_peer_profiles.py'),
            str(self.registry.path),str(Path(self.temporary.name)/'state'),'attachments'],
            env={'AGENT_TELEGRAM_HOME':str(Path(self.temporary.name)/'home')})
        with open(os.devnull,'w') as err:
            async with stdio_client(params,errlog=err) as (reader,writer):
                async with ClientSession(reader,writer) as session:
                    await session.initialize()
                    result=await session.call_tool('download_attachment',{'chat_id':11,'message_id':5})
                    self.assertFalse(result.isError)
                    meta=result.structuredContent
                    binary=next(c.resource for c in result.content if c.type=='resource')
                    data=base64.b64decode(binary.blob)
                    self.assertTrue(data.startswith(b'%PDF-'));self.assertGreater(len(data),65536)
                    self.assertEqual(hashlib.sha256(data).hexdigest(),meta['sha256'])
                    received=bytearray();offset=0
                    while True:
                        part=await session.call_tool('read_attachment_chunk',{'staging_id':meta['staging_id'],'offset':offset})
                        value=part.structuredContent
                        received.extend(base64.b64decode(value['data_base64']));offset=value['next_offset']
                        if value['complete']:break
                    self.assertEqual(bytes(received),data)
                    resource=await session.read_resource(meta['resource_uri'])
                    self.assertEqual(base64.b64decode(resource.contents[0].blob),data)
                    self.data['agents']['a']['actions'].remove('read-attachments');self.registry.save(self.data)
                    self.assertTrue((await session.call_tool('get_attachment_original',{'staging_id':meta['staging_id']})).isError)
