"""Read-only peer cards/avatars, using synthetic Telegram objects and image bytes."""
import asyncio
import base64
from datetime import datetime,timezone
import hashlib
import io
from pathlib import Path
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from PIL import Image
from telethon.tl import types
from telethon.errors import FileReferenceExpiredError

from telegram_setup.agents import AccessDenied
from telegram_setup.file_transport import StagedFiles
from telegram_setup.gateway import create_gateway
from telegram_setup.peer_profiles import PeerBackend,peer_policy
from telegram_setup.peer_gateway import PeerGateway
from telegram_setup.reader_gateway import ReaderGateway
from test_agent_gateway import RegistryFixture


def fixture_bytes():
    out=io.BytesIO();Image.new('RGB',(64,48),'#ea9911').save(out,'JPEG');return out.getvalue()


class PeerClient:
    def __init__(self):
        self.data=fixture_bytes();self.entities=[];self.requests=[];self.downloads=0
        self.partial=False;self.error=None;self.after_download=None
        self.photo=types.Photo(id=801,access_hash=0,file_reference=b'synthetic',date=datetime.now(timezone.utc),
            sizes=[types.PhotoSize(type='x',w=64,h=48,size=len(self.data))],dc_id=1)
        self.user=types.User(id=11,first_name='Synthetic',last_name='Peer',username='fixture',phone='not-exported',
            photo=types.UserProfilePhoto(photo_id=801,dc_id=1),contact=True)
        self.group=types.Chat(id=77,title='Synthetic group',photo=types.ChatPhoto(photo_id=801,dc_id=1),
            participants_count=3,date=datetime.now(timezone.utc),version=1)

    async def get_entity(self,peer):
        self.entities.append(peer)
        return self.group if peer==-77 else self.user

    async def __call__(self,request,**kwargs):
        self.requests.append(request)
        assert kwargs=={'flood_sleep_threshold':0}
        name=type(request).__name__
        if name=='GetUserPhotosRequest':
            photos=[self.photo] if request.offset in (0,-1) and request.max_id in (0,801) else []
            return NS(photos=photos,count=1)
        if name=='GetFullUserRequest':
            return NS(full_user=NS(about='Untrusted profile text',common_chats_count=2,personal_channel_id=None))
        if name=='GetFullChatRequest':return NS(full_chat=NS(about='Group bio',chat_photo=self.photo,participants_count=3))
        raise AssertionError(name)

    async def download_media(self,photo,*,file,progress_callback):
        self.downloads+=1
        if self.error:
            error,self.error=self.error,None;raise error
        file.write(self.data[:-1] if self.partial else self.data)
        if self.after_download:self.after_download()


class PeerBridge:
    available_tools={'read_agent_peer_profile','list_agent_avatars','download_agent_avatar'}
    def __init__(self,backend):self.backend,self.calls,self.flood=backend,[],False
    async def call(self,name,args):
        self.calls.append((name,args))
        if self.flood:return {'error':'telegram_cooldown','retry_after_seconds':23}
        if name=='read_agent_peer_profile':return await self.backend.profile(args['agent_id'],args['peer'])
        if name=='list_agent_avatars':return await self.backend.avatars(args['agent_id'],args['peer'],args['offset'],args['limit'])
        if name=='download_agent_avatar':return await self.backend.avatar(args['agent_id'],args['peer'],args['photo_id'])
        if name=='download_agent_attachment':return await self.backend.attachment(args['agent_id'],args['peer'],args['message_id'])
        raise AssertionError(name)


class PeerTests(RegistryFixture,unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_registry();self.data['agents']['a']['actions']+=['read-profiles','read-avatars']
        self.registry.save(self.data);self.store=StagedFiles(Path(self.temporary.name)/'staged')
        self.client=PeerClient();self.backend=PeerBackend(self.client,self.registry,self.store)
        self.bridge=PeerBridge(self.backend);self.api=ReaderGateway(self.registry,self.bridge)
        self.gateway=PeerGateway(self.api,self.store)
    def tearDown(self):self.teardown_registry()

    async def test_profile_field_whitelist_phone_preserved_policy_no_access_hash(self):
        result=await self.backend.profile('a',11)
        self.assertEqual(result['peer_id'],11);self.assertEqual(result['about'],'Untrusted profile text')
        self.assertNotIn('phone',result);self.assertNotIn('access_hash',result)
        self.assertTrue(result['untrusted_content'])
        self.data['agents']['a']['include_phones']=True;self.registry.save(self.data)
        self.assertEqual((await self.backend.profile('a',11))['phone'],'not-exported')

    async def test_avatar_real_pixels_original_hash_and_peer_binding(self):
        result=await self.gateway.avatar('a',11)
        metadata=result.structuredContent
        self.assertEqual(metadata['photo_id'],801);self.assertEqual(metadata['peer_id'],11)
        image=next(c for c in result.content if c.type=='image')
        self.assertEqual(hashlib.sha256(base64.b64decode(image.data)).hexdigest(),metadata['preview_sha256'])
        original=self.gateway.original('a',metadata['staging_id'])
        self.assertEqual(base64.b64decode(original.content[1].resource.blob),self.client.data)
        self.assertFalse(metadata['actual_agent_pixels_verified']);self.assertFalse(metadata['native_cloud_file_verified'])

    async def test_no_manual_grant_for_each_photo_and_previous_photo_id_exact_request(self):
        result=await self.backend.avatar('a',11,801)
        self.assertEqual(result['photo_id'],801)
        request=self.client.requests[0]
        self.assertEqual((request.offset,request.max_id,request.limit),(-1,801,1))
        with self.assertRaises(ValueError):await self.backend.avatar('a',11,999)

    async def test_avatar_listing_pagination_and_no_download(self):
        result=await self.backend.avatars('a',11,0,1)
        self.assertEqual(result['avatars'][0]['photo_id'],801)
        self.assertIsNone(result['next_offset']);self.assertEqual(self.client.downloads,0)
        for offset,limit in [(True,1),(0,True),(-1,5),(0,26)]:
            with self.assertRaises(ValueError):await self.backend.avatars('a',11,offset,limit)

    async def test_current_group_avatar_without_reading_chat_messages(self):
        self.data['agents']['a']['chat_ids'].append(-77);self.registry.save(self.data)
        profile=await self.backend.profile('a',-77);self.assertEqual(profile['type'],'group')
        listing=await self.backend.avatars('a',-77)
        self.assertFalse(listing['history_supported']);self.assertEqual(len(listing['avatars']),1)
        result=await self.backend.avatar('a',-77);self.assertEqual(result['peer_id'],-77)
        self.assertTrue(all(type(r).__name__=='GetFullChatRequest' for r in self.client.requests))

    async def test_unpermitted_peer_and_username_escape_denied_before_rpc(self):
        for peer in (22,'@fixture','https://example.com','/etc/passwd',True,0):
            with self.assertRaises((AccessDenied,ValueError)):await self.backend.profile('a',peer)
        self.assertEqual(self.client.entities,[])

    async def test_exact_username_full_scope_only(self):
        self.data['agents']['a']['all_chats']=True;self.registry.save(self.data)
        self.assertEqual((await self.backend.profile('a','@fixture'))['peer_id'],11)

    async def test_explicit_contact_id_is_allowed_without_expanding_all_contacts(self):
        self.data['agents']['a']['chat_ids']=[];self.data['agents']['a']['contact_ids']=[11];self.registry.save(self.data)
        self.assertEqual((await self.backend.profile('a',11))['peer_id'],11)

    async def test_partial_and_oversize_rejected(self):
        self.client.partial=True
        with self.assertRaisesRegex(ValueError,'partial'):await self.backend.avatar('a',11)
        self.assertFalse(self.store.folder.exists())
        self.client.partial=False;self.client.photo.sizes[0].size=self.store.maximum+1
        with self.assertRaisesRegex(ValueError,'exceeded'):await self.backend.avatar('a',11)

    async def test_refreshed_file_reference_reuses_same_photo(self):
        self.client.error=FileReferenceExpiredError(None)
        result=await self.backend.avatar('a',11)
        self.assertEqual(result['photo_id'],801);self.assertEqual(self.client.downloads,2)
        self.assertEqual(self.client.requests[-1].max_id,801)

    async def test_revocation_after_download_blocks_staging(self):
        def revoke():
            self.data['agents']['a']['actions'].remove('read-avatars');self.registry.save(self.data)
        self.client.after_download=revoke
        with self.assertRaises(AccessDenied):await self.backend.avatar('a',11)
        self.assertFalse(self.store.folder.exists())

    async def test_cached_original_revocation_ttl_and_other_agent(self):
        result=await self.backend.avatar('a',11);sid=result['staging_id']
        with self.assertRaises(PermissionError):self.gateway.original('b',sid)
        with patch('telegram_setup.file_transport.time.time',return_value=time.time()+1000):
            with self.assertRaises(PermissionError):self.gateway.original('a',sid)
        self.data['agents']['a']['actions'].remove('read-avatars');self.registry.save(self.data)
        with self.assertRaises(AccessDenied):self.gateway.original('a',sid)

    async def test_shared_gateway_cooldown_prevents_second_call(self):
        self.bridge.flood=True
        for _ in range(2):
            with self.assertRaisesRegex(Exception,'retry_after_seconds'):
                await self.gateway.call('a','read-profiles','read_agent_peer_profile',11)
        self.assertEqual(len(self.bridge.calls),1)

    async def test_catalog_default_unchanged_new_tools_all_read_only(self):
        server=create_gateway(self.registry,'a',enable_saved_text=True,enable_search=True,enable_file_probe=True)
        self.assertEqual(len(await server.list_tools()),10)
        server=create_gateway(self.registry,'a',enable_profiles=True)
        tools={t.name:t for t in await server.list_tools()}
        self.assertEqual(len(tools),10)
        for name in ('get_peer_profile','list_avatars','read_avatar','get_avatar_original'):
            self.assertTrue(tools[name].annotations.readOnlyHint)
        with self.assertRaisesRegex(ValueError,'fixed_stdio'):create_gateway(self.registry,enable_profiles=True)
