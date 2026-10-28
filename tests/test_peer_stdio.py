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


class PeerStdioTests(RegistryFixture,unittest.IsolatedAsyncioTestCase):
    def setUp(self):self.setup_registry()
    def tearDown(self):self.teardown_registry()
    async def test_real_stdio_grants_pixels_resource_and_revoke(self):
        params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'tests/offline_peer_profiles.py'),
            str(self.registry.path),str(Path(self.temporary.name)/'state')],
            env={'AGENT_TELEGRAM_HOME':str(Path(self.temporary.name)/'home')})
        with open(os.devnull,'w') as err:
            async with stdio_client(params,errlog=err) as (reader,writer):
                async with ClientSession(reader,writer) as session:
                    await session.initialize()
                    tools={t.name:t for t in (await session.list_tools()).tools}
                    self.assertEqual(len(tools),10)
                    denied=await session.call_tool('get_peer_profile',{'peer':11});self.assertTrue(denied.isError)
                    self.data['agents']['a']['actions']+=['read-profiles','read-avatars'];self.registry.save(self.data)
                    card=await session.call_tool('get_peer_profile',{'peer':11})
                    self.assertFalse(card.isError);self.assertNotIn('phone',card.structuredContent)
                    avatars=await session.call_tool('list_avatars',{'peer':11})
                    self.assertEqual(avatars.structuredContent['avatars'][0]['photo_id'],801)
                    result=await session.call_tool('read_avatar',{'peer':11})
                    self.assertFalse(result.isError);self.assertTrue(any(c.type=='image' for c in result.content))
                    meta=result.structuredContent;resource=await session.read_resource(meta['original_resource_uri'])
                    self.assertEqual(hashlib.sha256(base64.b64decode(resource.contents[0].blob)).hexdigest(),meta['sha256'])
                    for args in ({'peer':11,'path':'/etc/passwd'},{'peer':11,'agent':'b'},{'peer':True}):
                        self.assertTrue((await session.call_tool('read_avatar',args)).isError)
                    self.data['agents']['a']['actions'].remove('read-avatars');self.registry.save(self.data)
                    self.assertTrue((await session.call_tool('get_avatar_original',{'staging_id':meta['staging_id']})).isError)
                    with self.assertRaises(Exception):await session.read_resource(meta['original_resource_uri'])
