"""Synthetic images and fake Telegram only; no owner files or network reads."""
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from PIL import Image
from telethon.errors import FileReferenceExpiredError, FloodWaitError, ChannelPrivateError

from telegram_setup.agents import AccessDenied, check_photo
from telegram_setup.file_transport import StagedFiles
from telegram_setup.gateway import create_gateway
from telegram_setup.photo_gateway import PhotoGateway, PHOTO_RESOURCE
from telegram_setup.photos import PhotoBackend, image_preview, synthetic_image_probe, register_photos
from test_agent_gateway import RegistryFixture


def image_bytes(format='JPEG', size=(60, 40)):
    target = io.BytesIO()
    Image.new('RGB', size, '#ee9933').save(target, format)
    return target.getvalue()


class PhotoClient:
    def __init__(self, *, document=False, data=None):
        self.data = data if data is not None else image_bytes('PNG' if document else 'JPEG')
        self.reads, self.downloads = [], 0
        self.error, self.after_download = None, None
        self.partial, self.overshoot, self.refresh = False, False, False
        self.message = NS(id=5, chat_id=11, photo=None if document else NS(id=801),
            document=NS(id=802, mime_type='image/png', attributes=[]) if document else None,
            grouped_id=90, file=NS(size=len(self.data)), noforwards=False)

    async def get_messages(self, chat, ids):
        self.reads.append((chat, ids))
        return self.message

    async def download_media(self, message, *, file, progress_callback):
        self.downloads += 1
        if self.error:
            error, self.error = self.error, None
            raise error
        if self.overshoot:
            progress_callback(10000, 10000)
        file.write(self.data[:-1] if self.partial else self.data)
        if self.after_download:
            self.after_download()


class PhotoBridge:
    available_tools = frozenset({'download_scoped_photo'})
    send_text_available = False
    def __init__(self, backend): self.backend, self.calls, self.flood = backend, [], False
    async def call(self, name, args):
        self.calls.append((name, args))
        assert name == 'download_scoped_photo'
        if self.flood: return {'error': 'telegram_cooldown', 'retry_after_seconds': 23}
        return await self.backend.read(args['agent_id'], args['chat_id'], args['message_id'])


class PhotoTests(RegistryFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_registry()
        self.data['agents']['a']['actions'] += ['read-photo']
        self.data['agents']['a']['photo_sources'] = [{'chat_id': 11, 'message_id': 5}]
        self.registry.save(self.data)
        self.store = StagedFiles(Path(self.temporary.name) / 'files')
        self.client = PhotoClient()
        self.backend = PhotoBackend(self.client, self.registry, self.store)
        self.bridge = PhotoBridge(self.backend)
        self.api = PhotoGateway(self.registry, self.bridge, Path(self.temporary.name) / 'receipts', self.store)

    def tearDown(self): self.teardown_registry()

    async def test_photo_and_preview_are_real_bytes_original_hash_and_album_not_expanded(self):
        result = await self.api.read_photo('a', 11, 5)
        meta = result.structuredContent
        self.assertEqual(meta['original_meaning'], 'largest_telegram_representation')
        self.assertTrue(meta['downloaded_to_linux'] and meta['exposed_by_mcp'] and meta['verified_original_sha'])
        self.assertFalse(meta['actual_dot_can_see_pixels_or_file'])
        preview = next(c for c in result.content if c.type == 'image')
        data = base64.b64decode(preview.data)
        self.assertEqual(hashlib.sha256(data).hexdigest(), meta['preview_sha256'])
        with Image.open(io.BytesIO(data)) as image: self.assertEqual(image.size, (60, 40))
        original = self.api.photo_original('a', meta['staging_id'])
        self.assertEqual(base64.b64decode(original.content[1].resource.blob), self.client.data)
        self.assertEqual(meta['sha256'], hashlib.sha256(self.client.data).hexdigest())
        self.assertEqual(self.client.reads, [(11, 5), (11, 5)])
        self.assertEqual(self.client.downloads, 1)

    async def test_image_document_keeps_original_png_bytes(self):
        self.backend.client = PhotoClient(document=True)
        result = await self.api.read_photo('a', 11, 5)
        meta = result.structuredContent
        self.assertEqual(meta['mime_type'], 'image/png')
        self.assertEqual(meta['original_meaning'], 'image_document_bytes')
        self.assertEqual(self.api.photo_bytes('a', meta['staging_id'])[1], self.backend.client.data)

    async def test_exact_scope_required_even_with_all_chats(self):
        self.data['agents']['a']['all_chats'] = True; self.registry.save(self.data)
        for chat, msg in [(11, 6), (22, 5), (True, 5), (11, True)]:
            with self.assertRaises(AccessDenied): await self.api.read_photo('a', chat, msg)
        self.assertEqual(self.bridge.calls, [])

    async def test_default_reader_agent_has_no_photo_authority(self):
        with self.assertRaises(AccessDenied): await self.api.read_photo('b', 22, 5)
        self.assertEqual(self.client.reads, [])

    async def test_revoke_and_agent_binding_and_ttl_block_stored_resources(self):
        result = await self.api.read_photo('a', 11, 5); sid = result.structuredContent['staging_id']
        with self.assertRaises(PermissionError): self.api.photo_bytes('b', sid)
        self.data['agents']['a']['photo_sources'] = []; self.registry.save(self.data)
        with self.assertRaises(AccessDenied): self.api.photo_original('a', sid)
        self.data['agents']['a']['photo_sources'] = [{'chat_id': 11, 'message_id': 5}]; self.registry.save(self.data)
        with patch('telegram_setup.file_transport.time.time', return_value=time.time() + 1000):
            with self.assertRaises(PermissionError): self.api.photo_bytes('a', sid)

    async def test_partial_download_is_never_staged(self):
        self.client.partial = True
        with self.assertRaisesRegex(ValueError, 'partial'): await self.backend.read('a', 11, 5)
        self.assertFalse(self.store.folder.exists())

    async def test_declared_oversize_and_actual_overshoot(self):
        self.client.message.file.size = self.store.maximum + 1
        with self.assertRaisesRegex(ValueError, 'size_exceeded'): await self.backend.read('a', 11, 5)
        self.assertEqual(self.client.downloads, 0)
        self.client.message.file.size = len(self.client.data); self.client.overshoot = True
        with self.assertRaisesRegex(ValueError, 'size_exceeded'): await self.backend.read('a', 11, 5)

    async def test_deleted_wrong_chat_video_and_protected_messages(self):
        original = self.client.message
        for message in (None, NS(id=5, chat_id=22), NS(id=6, chat_id=11),
                        NS(id=5, chat_id=11, photo=None, document=None),
                        NS(id=5, chat_id=11, noforwards=True)):
            self.client.message = message
            with self.assertRaises(ValueError): await self.backend.read('a', 11, 5)
        self.client.message = original
        self.assertEqual(self.client.downloads, 0)

    async def test_refresh_same_source_once_without_sibling_search(self):
        self.client.error = FileReferenceExpiredError(None)
        result = await self.backend.read('a', 11, 5)
        self.assertTrue(result['downloaded_to_linux'])
        self.assertEqual(self.client.downloads, 2)
        self.assertEqual(self.client.reads, [(11, 5)] * 3)

    async def test_second_expired_reference_fails_after_one_refresh(self):
        async def expired(*args, **kwargs):
            self.client.downloads += 1
            raise FileReferenceExpiredError(None)
        self.client.download_media = expired
        with self.assertRaises(FileReferenceExpiredError): await self.backend.read('a', 11, 5)
        self.assertEqual(self.client.downloads, 2)
        self.assertEqual(self.client.reads, [(11, 5)] * 2)
        self.assertFalse(self.store.folder.exists())

    async def test_timeout_has_no_staging_or_automatic_retry(self):
        import asyncio
        original = asyncio.wait_for
        async def slow(*args, **kwargs):
            self.client.downloads += 1
            await asyncio.sleep(1)
        async def bounded(task, timeout): return await original(task, .01)
        self.client.download_media = slow
        with patch('telegram_setup.photos.asyncio.wait_for', side_effect=bounded):
            with self.assertRaises(TimeoutError): await self.backend.read('a', 11, 5)
        self.assertEqual(self.client.downloads, 1)
        self.assertFalse(self.store.folder.exists())

    async def test_cli_grant_revoke_preserves_other_authority_and_identity(self):
        import copy
        from telegram_setup.agents import main
        self.data['agents']['a']['actions'].remove('read-photo')
        self.data['agents']['a'].pop('photo_sources')
        self.registry.save(self.data)
        before = copy.deepcopy(self.registry.load())
        for argv in (['grant-read-photo', 'a', '--chat-id', '11', '--message-id', '5'],
                     ['revoke-read-photo', 'a']):
            with patch.object(sys, 'argv', ['agents', '--registry', str(self.registry.path)] + argv):
                with contextlib.redirect_stdout(io.StringIO()): main()
            after = self.registry.load()
            self.assertEqual(after['agents']['b'], before['agents']['b'])
            for key in ('token_sha256', 'expires_at', 'all_chats', 'chat_ids'):
                self.assertEqual(after['agents']['a'][key], before['agents']['a'][key])
        self.assertEqual(self.registry.load(), before)

    async def test_large_original_uses_real_resource_link_not_fake_file_id(self):
        target = io.BytesIO()
        Image.effect_noise((500, 500), 90).convert('RGB').save(target, 'PNG')
        self.backend.client = PhotoClient(document=True, data=target.getvalue())
        result = await self.api.read_photo('a', 11, 5)
        original = self.api.photo_original('a', result.structuredContent['staging_id'])
        self.assertEqual(original.content[1].type, 'resource_link')
        self.assertEqual(self.api.photo_bytes('a', result.structuredContent['staging_id'])[1], target.getvalue())
        self.assertNotIn('file_id', original.structuredContent)
        self.assertEqual(original.structuredContent['host_artifact_creation'], 'unverified')

    async def test_changed_source_during_download_rejected(self):
        def changed(): self.client.message.photo.id += 1
        self.client.after_download = changed
        with self.assertRaisesRegex(ValueError, 'source_changed'): await self.backend.read('a', 11, 5)
        self.assertFalse(self.store.folder.exists())

    async def test_changed_source_during_reference_refresh_rejected(self):
        original_get = self.client.get_messages
        async def changed(chat, ids):
            if self.client.reads:
                self.client.message.photo.id += 1
            return await original_get(chat, ids)
        self.client.get_messages = changed
        self.client.error = FileReferenceExpiredError(None)
        with self.assertRaisesRegex(ValueError, 'source_changed'): await self.backend.read('a', 11, 5)
        self.assertEqual(self.client.downloads, 1)
        self.assertFalse(self.store.folder.exists())

    async def test_revocation_during_download_blocks_publication(self):
        def revoke():
            self.data['agents']['a']['actions'].remove('read-photo'); self.registry.save(self.data)
        self.client.after_download = revoke
        with self.assertRaises(AccessDenied): await self.backend.read('a', 11, 5)
        self.assertFalse(self.store.folder.exists())

    async def test_invalid_mime_and_untrusted_bytes(self):
        for data, mime in [(b'not an image', 'image/jpeg'), (image_bytes('PNG'), 'image/jpeg'),
                           (image_bytes('GIF'), 'image/gif'), (image_bytes(size=(12001, 1)), 'image/jpeg')]:
            with self.assertRaises(ValueError): image_preview(data, mime)
        self.client.data = b'not an image'; self.client.message.file.size = len(self.client.data)
        with self.assertRaises(ValueError): await self.backend.read('a', 11, 5)

    async def test_tampered_original_hash_and_arbitrary_path_denied(self):
        result = await self.api.read_photo('a', 11, 5); sid = result.structuredContent['staging_id']
        (self.store.entry(sid) / 'blob').write_bytes(b'bad')
        with self.assertRaisesRegex(ValueError, 'integrity'): self.api.photo_bytes('a', sid)
        for path in ('../../secret', 'https://example.com/photo', '/etc/passwd'):
            with self.assertRaises(ValueError): self.api.photo_bytes('a', path)

    async def test_floodwait_cooldown_prevents_repeat_upstream(self):
        self.bridge.flood = True
        for _ in range(2):
            with self.assertRaisesRegex(Exception, 'retry_after_seconds'): await self.api.read_photo('a', 11, 5)
        self.assertEqual(len(self.bridge.calls), 1)
        self.assertEqual(self.client.reads, [])

    async def test_backend_surface_floodwait_and_private_errors_no_secret_text(self):
        tools = {}
        class FakeMCP:
            def tool(self, **kwargs):
                def register(fn): tools[fn.__name__] = fn; return fn
                return register
        runtime = NS(mcp=FakeMCP(), ToolAnnotations=lambda **kw: kw,
                     with_account=lambda **kw: lambda f: f, get_client=lambda account: self.client)
        with patch.dict(os.environ, {'AGENT_TELEGRAM_HOME': self.temporary.name}):
            register_photos(runtime)
        self.client.error = FloodWaitError(None, capture=27)
        result = await tools['download_scoped_photo']('a', 11, 5)
        self.assertEqual(result, {'error': 'telegram_cooldown', 'retry_after_seconds': 27})
        again = await tools['download_scoped_photo']('a', 11, 5)
        self.assertEqual(again['error'], 'telegram_cooldown'); self.assertEqual(self.client.downloads, 1)
        # A separate account key tests the RPC failure path without waiting.
        self.client.error = ChannelPrivateError(None)
        result = await tools['download_scoped_photo']('a', 11, 5, account='synthetic')
        self.assertEqual(result['failure_category'], 'source_or_media_unavailable')
        self.assertNotIn('ChannelPrivateError', json.dumps(result))

    async def test_defaults_unchanged_probe_no_data_reads_strict_input(self):
        @contextlib.asynccontextmanager
        async def upstream(): yield self.bridge
        default = create_gateway(self.registry, 'a', upstream)
        self.assertEqual(len(await default.list_tools()), 6)
        server = create_gateway(self.registry, 'a', upstream, enable_image_probe=True)
        async with server._mcp_server.lifespan(server._mcp_server):
            result = await server._tool_manager.call_tool('image_transport_probe', {})
            self.assertEqual(result.content[1].type, 'image')
            self.assertEqual(result.structuredContent['telegram_calls'], 0)
            with self.assertRaises(Exception):
                await server._tool_manager.call_tool('image_transport_probe', {'path': '/etc/passwd'})
        self.assertEqual(self.bridge.calls, [])
        with self.assertRaisesRegex(ValueError, 'fixed_stdio'): create_gateway(self.registry, enable_read_photo=True)
