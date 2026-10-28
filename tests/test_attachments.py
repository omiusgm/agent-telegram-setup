"""Actual binary attachment transfer; synthetic files only, never private Telegram."""
import base64
import hashlib
from pathlib import Path
import unittest
from types import SimpleNamespace as NS

from telethon.tl import types
from telegram_setup.attachment_gateway import AttachmentGateway
from telegram_setup.agents import AccessDenied
from test_peer_profiles import PeerClient,fixture_bytes
import test_peer_profiles as peer_fixtures
from test_agent_gateway import RegistryFixture


class AttachmentClient(PeerClient):
    def __init__(self):
        super().__init__()
        self.data=b'%PDF-1.4\n'+b'x'*900000+b'\n%%EOF'
        self.message=NS(id=5,chat_id=11,noforwards=False,photo=None,
            document=NS(id=99),media=types.MessageMediaDocument(),
            file=NS(size=len(self.data),name='fixture.pdf',mime_type='application/pdf'))
    async def get_messages(self,entity,ids):return self.message


class AttachmentTests(RegistryFixture,unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        peer_fixtures.PeerTests.setUp(self)
        self.data['agents']['a']['actions'].append('read-attachments');self.registry.save(self.data)
        self.client=AttachmentClient();self.backend.client=self.client
        self.bridge.available_tools=set(self.bridge.available_tools)|{'download_agent_attachment'}
        self.attachment=AttachmentGateway(self.api,self.store)
    def tearDown(self):self.teardown_registry()

    async def test_pdf_larger_than_64k_contains_actual_bytes_not_resource_link(self):
        result=await self.attachment.download('a',11,5)
        resource=next(c.resource for c in result.content if c.type=='resource')
        returned=base64.b64decode(resource.blob)
        self.assertEqual(returned,self.client.data);self.assertEqual(resource.mimeType,'application/pdf')
        self.assertEqual(hashlib.sha256(returned).hexdigest(),result.structuredContent['sha256'])
        self.assertFalse(any(c.type=='resource_link' for c in result.content))
        self.assertFalse(result.structuredContent['actual_agent_file_verified'])

    async def test_binary_chunks_reconstruct_exact_file_and_support_resumed_offsets(self):
        result=await self.backend.attachment('a',11,5);sid=result['staging_id']
        first=self.attachment.chunk('a',sid,0,49152)
        chunks=[base64.b64decode(first['data_base64'])];offset=first['next_offset']
        while offset<len(self.client.data):
            item=self.attachment.chunk('a',sid,offset,49152)
            chunks.append(base64.b64decode(item['data_base64']));offset=item['next_offset']
        self.assertEqual(b''.join(chunks),self.client.data)
        self.assertTrue(item['complete'])

    async def test_deleted_protected_wrong_message_and_partial_never_exposed(self):
        original=self.client.message
        for value in (None,NS(id=6,chat_id=11),NS(id=5,chat_id=22),NS(id=5,chat_id=11,noforwards=True)):
            self.client.message=value
            with self.assertRaises(ValueError):await self.backend.attachment('a',11,5)
        self.client.message=original;self.client.partial=True
        with self.assertRaisesRegex(ValueError,'partial'):await self.backend.attachment('a',11,5)
        self.assertFalse(self.store.folder.exists())

    async def test_photo_has_pixels_and_original_embedded_bytes(self):
        self.client.data=fixture_bytes();m=self.client.message
        m.media=types.MessageMediaPhoto(photo=self.client.photo);m.photo=self.client.photo;m.document=None
        m.file=NS(size=len(self.client.data),name=None,mime_type='image/jpeg')
        result=await self.attachment.download('a',11,5)
        self.assertTrue(any(c.type=='image' for c in result.content))
        self.assertEqual(base64.b64decode(result.content[1].resource.blob),self.client.data)

    async def test_original_and_chunks_deny_other_agent_revocation_and_paths(self):
        result=await self.backend.attachment('a',11,5);sid=result['staging_id']
        with self.assertRaises(AccessDenied):self.attachment.chunk('b',sid,0,100)
        with self.assertRaises(ValueError):self.attachment.chunk('a','../../x',0,100)
        for offset,limit in ((True,3),(0,0),(0,50000),(len(self.client.data)+1,1)):
            with self.assertRaises(ValueError):self.attachment.chunk('a',sid,offset,limit)
        self.data['agents']['a']['actions'].remove('read-attachments');self.registry.save(self.data)
        with self.assertRaises(AccessDenied):self.attachment.original('a',sid)

    async def test_staging_kind_cannot_reuse_avatar_as_attachment(self):
        self.backend.client=PeerClient()
        avatar=await self.backend.avatar('a',11)
        with self.assertRaises(PermissionError):self.attachment.original('a',avatar['staging_id'])
