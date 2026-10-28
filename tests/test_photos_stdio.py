"""End-to-end SDK image content and original resource, not a cloud acceptance claim."""
import base64
import hashlib
import io
import os
from pathlib import Path
import sys
import unittest

from PIL import Image
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from telegram_setup.config import ROOT
from test_agent_gateway import RegistryFixture
from test_photos import image_bytes


class PhotoStdioTests(RegistryFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self): self.setup_registry()
    def tearDown(self): self.teardown_registry()

    async def test_actual_stdio_pixels_original_resource_and_live_revoke(self):
        params = StdioServerParameters(command=sys.executable,
            args=[str(ROOT / 'tests/offline_photos.py'), str(self.registry.path),
                  str(Path(self.temporary.name) / 'state')],
            env={'AGENT_TELEGRAM_HOME': str(Path(self.temporary.name) / 'home')})
        with open(os.devnull, 'w') as stderr:
            async with stdio_client(params, errlog=stderr) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    tools = {t.name: t for t in (await session.list_tools()).tools}
                    self.assertEqual(len(tools), 9)
                    self.assertEqual(set(tools['read_photo'].inputSchema['properties']), {'chat_id', 'message_id'})
                    self.assertTrue(tools['read_photo'].annotations.readOnlyHint)
                    self.assertTrue((await session.call_tool('read_photo', {'chat_id': 11, 'message_id': 5})).isError)
                    probe = await session.call_tool('image_transport_probe', {})
                    image = next(c for c in probe.content if c.type == 'image')
                    self.assertEqual(image.mimeType, 'image/png')
                    pixels = base64.b64decode(image.data)
                    self.assertEqual(hashlib.sha256(pixels).hexdigest(), probe.structuredContent['sha256'])
                    with Image.open(io.BytesIO(pixels)) as img: self.assertEqual(img.size, (480, 320))
                    self.data['agents']['a']['actions'].append('read-photo')
                    self.data['agents']['a']['photo_sources'] = [{'chat_id': 11, 'message_id': 5}]
                    self.registry.save(self.data)
                    result = await session.call_tool('read_photo', {'chat_id': 11, 'message_id': 5})
                    self.assertFalse(result.isError)
                    self.assertTrue(any(c.type == 'image' for c in result.content))
                    metadata = result.structuredContent
                    original = await session.read_resource(metadata['original_resource_uri'])
                    original_bytes = base64.b64decode(original.contents[0].blob)
                    self.assertEqual(original_bytes, image_bytes())
                    self.assertEqual(metadata['sha256'], hashlib.sha256(original_bytes).hexdigest())
                    self.assertFalse(metadata['actual_dot_can_see_pixels_or_file'])
                    # Hidden caller-selected destination/path must be rejected before dispatch.
                    bad = await session.call_tool('read_photo', {'chat_id': 11, 'message_id': 5, 'path': '/etc/passwd'})
                    self.assertTrue(bad.isError)
                    self.data['agents']['a']['photo_sources'] = []; self.registry.save(self.data)
                    with self.assertRaises(Exception): await session.read_resource(metadata['original_resource_uri'])
                    self.assertTrue((await session.call_tool('get_photo_original',
                        {'staging_id': metadata['staging_id']})).isError)
