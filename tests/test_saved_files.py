"""Only synthetic bytes and fake Telegram. No login, live sends or private files."""
import asyncio
import contextlib
import hashlib
import io
import ipaddress
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
import copy
from types import SimpleNamespace as NS
from unittest.mock import patch

from mcp.types import CallToolResult

from telegram_setup.agents import AccessDenied
from telegram_setup.file_gateway import FileGateway
from telegram_setup.file_transport import OpenAIFile, StagedFiles, allowed_url, fetch_host_file
from telegram_setup.gateway import create_gateway
from telegram_setup.saved_files import SavedFileBackend
from telegram_setup.saved_text import SendReceipts
from test_agent_gateway import RegistryFixture, FakeUpstream

FIXTURE = b'Telegram MCP synthetic byte fixture\nNo personal information.\n'
SHA = hashlib.sha256(FIXTURE).hexdigest()


class FileClient:
    def __init__(self):
        self.sent = []; self.reads = []; self.bad_bytes = False; self.fail_after_send = False
        self.after_download = None

    async def get_me(self): return NS(id=123456, bot=False)

    async def send_file(self, peer, source, **kwargs):
        assert type(peer).__name__ == 'InputPeerSelf'
        assert kwargs['force_document'] and kwargs['parse_mode'] is None
        self.sent.append((source.read(), source.name, kwargs))
        if self.fail_after_send: raise TimeoutError('synthetic lost send reply')
        return NS(id=501)

    async def get_messages(self, peer, ids):
        assert type(peer).__name__ == 'InputPeerSelf'
        self.reads.append(ids)
        if not self.sent: return None
        data, name, kwargs = self.sent[-1]
        return NS(id=ids, chat_id=123456, document=True, message=kwargs['caption'],
                  file=NS(size=len(data), name=name, mime_type=kwargs['mime_type']))

    async def download_media(self, message, file, progress_callback):
        data = self.sent[-1][0]
        if self.bad_bytes: data = b'x' * len(data)
        progress_callback(len(data), len(data)); file.write(data)
        if self.after_download: self.after_download()


class BackendBridge(FakeUpstream):
    saved_files_available = True
    send_text_available = True
    def __init__(self, backend): super().__init__(); self.backend = backend

    async def call(self, name, args):
        self.calls.append((name, args))
        if name == 'send_staged_saved_file':
            args = dict(args); agent = args.pop('agent_id')
            return await self.backend.send(agent, **args)
        if name == 'download_saved_attachment':
            return await self.backend.download(args['agent_id'], args['message_id'])
        return await super().call(name, args)


class FileTests(RegistryFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from telethon.tl.types import InputPeerSelf
        self.setup_registry(); self.root = Path(self.temporary.name)
        self.store = StagedFiles(self.root / 'files')
        self.client = FileClient()
        self.backend = SavedFileBackend(self.client, self.registry, self.store,
                         SendReceipts(self.root / 'backend-receipts'), self_peer=InputPeerSelf())
        self.bridge = BackendBridge(self.backend)
        self.api = FileGateway(self.registry, self.bridge, self.root / 'gateway-receipts', self.store,
                               fetcher=lambda *_: FIXTURE, file_hosts=['uploads.example.test'])

    def tearDown(self): self.teardown_registry()

    def grant(self, agent='a'):
        self.data['agents'][agent]['all_chats'] = True
        self.data['agents'][agent]['actions'] += ['send-saved-file', 'download-saved-file']
        self.registry.save(self.data)

    async def stage(self):
        return await self.api.stage_file('a', OpenAIFile(download_url='https://uploads.example.test/synthetic',
            file_id='synthetic-file', file_name='fixture.txt', mime_type='text/plain'), len(FIXTURE), SHA)

    async def test_no_grant_no_network_no_file_no_send(self):
        with self.assertRaises(AccessDenied): await self.stage()
        with self.assertRaises(AccessDenied): await self.api.send_saved_file('a', 'x', 'file-001')
        with self.assertRaises(AccessDenied): await self.api.download_saved_file('a', 501)
        self.assertEqual(self.bridge.calls, []); self.assertEqual(self.client.sent, [])
        self.assertFalse(self.store.folder.exists())

    async def test_saved_send_download_back_resource_bytes_and_durable_retry(self):
        self.grant(); staged = await self.stage()
        self.assertEqual(staged['sha256'], SHA); self.assertFalse(staged['telegram_sent'])
        result = await self.api.send_saved_file('a', staged['staging_id'], 'file-request-001', 'fixture')
        self.assertTrue(result['readback_verified']); self.assertEqual(result['status'], 'delivered_verified')
        self.assertEqual(result['message_id'], 501)
        old_calls = len(self.bridge.calls)
        self.bridge.saved_files_available = False
        restarted = FileGateway(self.registry, self.bridge, self.root / 'gateway-receipts', self.store)
        self.assertEqual(await restarted.send_saved_file('a', staged['staging_id'], 'file-request-001', 'fixture'), result)
        self.assertEqual(len(self.bridge.calls), old_calls); self.assertEqual(len(self.client.sent), 1)
        with self.assertRaises(ValueError):
            await restarted.send_saved_file('a', staged['staging_id'], 'file-request-001', 'changed')
        self.bridge.saved_files_available = True
        output = await self.api.download_saved_file('a', 501)
        self.assertIsInstance(output, CallToolResult)
        self.assertFalse(output.structuredContent['dot_file_received_verified'])
        import base64
        resource = output.content[1].resource
        self.assertEqual(base64.b64decode(resource.blob), FIXTURE)
        self.assertEqual(self.api.resource_bytes('a', result['download_staging_id'])[1], FIXTURE)
        for path in self.root.rglob('*.json'): self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    async def test_wrong_agent_paths_symlink_expiry_and_corrupt_bytes_rejected(self):
        self.grant(); self.grant('b'); staged = await self.stage()
        with self.assertRaises(PermissionError): self.store.load('b', staged['staging_id'])
        for name in ('../agents.json', '/etc/passwd', 'x' * 33):
            with self.assertRaises(ValueError): self.store.load('a', name)
        directory = self.store.entry(staged['staging_id'])
        blob = directory / 'blob'; blob.unlink(); blob.symlink_to(self.registry.path)
        with self.assertRaises(OSError): self.store.load('a', staged['staging_id'])
        blob.unlink(); blob.write_bytes(b'x' * len(FIXTURE)); blob.chmod(0o600)
        with self.assertRaises(ValueError): self.store.load('a', staged['staging_id'])
        with patch('telegram_setup.file_transport.time.time', return_value=time.time() + 1000):
            with self.assertRaises(PermissionError): self.store.load('a', staged['staging_id'])
        self.assertEqual(self.client.sent, [])

    async def test_lost_response_and_download_mismatch_never_resend(self):
        self.grant()
        for i, mode in enumerate(('timeout', 'hash')):
            staged = await self.stage()
            self.client.fail_after_send = mode == 'timeout'; self.client.bad_bytes = mode == 'hash'
            result = await self.api.send_saved_file('a', staged['staging_id'], 'lost-request-' + str(i))
            self.assertEqual(result['status'], 'uncertain_do_not_retry')
            count = len(self.client.sent)
            self.assertEqual(await self.api.send_saved_file('a', staged['staging_id'], 'lost-request-' + str(i)), result)
            self.assertEqual(len(self.client.sent), count)

    async def test_saved_not_allowed_in_chat_scope_rejected_before_send(self):
        self.grant(); self.data['agents']['a']['all_chats'] = False; self.registry.save(self.data)
        staged = await self.stage()
        result = await self.api.send_saved_file('a', staged['staging_id'], 'not-allowed-saved')
        self.assertEqual(result['status'], 'uncertain_do_not_retry')
        self.assertEqual(self.client.sent, [])

    async def test_revoke_while_queued_and_after_download_blocks_outputs(self):
        self.grant(); staged = await self.stage()
        await self.api.lock.acquire()
        task = asyncio.create_task(self.api.send_saved_file('a', staged['staging_id'], 'queued-request-1'))
        await asyncio.sleep(0)
        self.data['agents']['a']['actions'].remove('send-saved-file'); self.registry.save(self.data)
        self.api.lock.release()
        with self.assertRaises(AccessDenied): await task
        self.assertEqual(self.client.sent, [])
        self.data['agents']['a']['actions'].append('send-saved-file'); self.registry.save(self.data)
        await self.api.send_saved_file('a', staged['staging_id'], 'setup-request-1')
        def revoke():
            self.data['agents']['a']['actions'].remove('download-saved-file'); self.registry.save(self.data)
        self.client.after_download = revoke
        with self.assertRaises(AccessDenied): await self.api.download_saved_file('a', 501)

    async def test_catalog_exact_file_schema_and_no_caller_recipient_path(self):
        @contextlib.asynccontextmanager
        async def upstream(): yield self.bridge
        server = create_gateway(self.registry, 'a', upstream, enable_saved_files=True,
                                enable_saved_text=True, enable_search=True,
                                file_store=self.store, receipt_dir=self.root / 'gateway-receipts')
        catalog = {t.name: t for t in await server.list_tools()}
        self.assertEqual(len(catalog), 14)
        tool = catalog['stage_file']; self.assertEqual(tool.meta['openai/fileParams'], ['file'])
        schema = tool.inputSchema['$defs']['OpenAIFile']
        self.assertEqual(set(schema['properties']), {'download_url', 'file_id', 'file_name', 'mime_type'})
        self.assertEqual(set(schema['required']), {'download_url', 'file_id'})
        self.assertEqual(catalog['send_saved_file'].inputSchema['properties'].keys(),
                         {'staging_id', 'request_id', 'caption'})
        for field in ('recipient', 'chat_id', 'account', 'file_path', 'agent_id'):
            with self.assertRaisesRegex(Exception, 'does_not_accept'):
                await server._tool_manager.call_tool('send_saved_file', {
                    'staging_id': 'x', 'request_id': 'request-001', field: 'attacker'})

    async def test_generated_text_fallback_limits_and_resource_requires_download_kind(self):
        self.grant()
        result = self.api.stage_text_file('a', 'Synthetic note\n', 'note.txt')
        self.assertFalse(result['native_host_input_verified'])
        with self.assertRaises(PermissionError): self.api.resource_bytes('a', result['staging_id'])
        for text, name in [('x' * 65537, 'note.txt'), ('note', '../../note.txt'), ('note', 'note.exe')]:
            with self.assertRaises(ValueError): self.api.stage_text_file('a', text, name)

    async def test_storage_size_and_backend_stream_limit_are_hard_limits(self):
        from telegram_setup.saved_files import BoundedBytesIO
        stream = BoundedBytesIO(4)
        with self.assertRaises(ValueError): stream.write(b'12345')
        self.assertEqual(stream.getvalue(), b'')
        store = StagedFiles(self.root / 'small-files', maximum=4)
        with self.assertRaises(ValueError): store.put('a', b'12345', 'file.txt', 'text/plain')
        self.assertFalse(store.folder.exists())

    async def test_file_cooldown_blocks_second_upstream_request(self):
        self.grant(); staged = await self.stage()
        self.api.retry_at = time.monotonic() + 30
        result = await self.api.send_saved_file('a', staged['staging_id'], 'cooldown-request-1')
        self.assertFalse(result['sent']); self.assertEqual(result['error'], 'telegram_cooldown')
        self.assertEqual(self.bridge.calls, [])

    async def test_minimal_cli_grants_and_revokes_preserve_all_other_profile_fields(self):
        from telegram_setup.agents import main
        for command, action in [('public-search', 'public-search'), ('saved-upload', 'send-saved-file'),
                                ('saved-download', 'download-saved-file')]:
            before = self.registry.load()
            with patch('sys.argv', ['agents', '--registry', str(self.registry.path), 'grant-' + command, 'a']), \
                    contextlib.redirect_stdout(io.StringIO()):
                main()
            granted = self.registry.load()
            expected = copy.deepcopy(before)
            expected['agents']['a']['actions'] = sorted(set(expected['agents']['a']['actions']) | {action})
            self.assertEqual(granted, expected)
            with patch('sys.argv', ['agents', '--registry', str(self.registry.path), 'revoke-' + command, 'a']), \
                    contextlib.redirect_stdout(io.StringIO()):
                main()
            self.assertEqual(self.registry.load(), before)
        self.assertEqual(self.client.sent, []); self.assertEqual(self.bridge.calls, [])


class FetchTests(unittest.TestCase):
    def test_transport_exception_never_echoes_temporary_signed_url(self):
        file = OpenAIFile(download_url='https://uploads.example.test/temporary?signature=synthetic', file_id='synthetic')
        class BrokenConnection:
            def __init__(self, *_): pass
            def request(self, *_args, **_kwargs): raise ValueError(file.download_url)
            def close(self): pass
        with patch('telegram_setup.file_transport.allowed_url', return_value=(NS(hostname='uploads.example.test', path='/temporary', query=''), [NS()])), \
                patch('telegram_setup.file_transport.PinnedHTTPS', BrokenConnection):
            with self.assertRaisesRegex(ValueError, '^file_host_transport_failed$'):
                fetch_host_file(file, len(FIXTURE), SHA, [])

    def test_file_schema_strict_and_url_host_dns_validation_no_network(self):
        with self.assertRaises(Exception): OpenAIFile(download_url='x', file_id='y', file_path='/etc/passwd')
        file = OpenAIFile(download_url='https://unknown.example.test/x', file_id='synthetic')
        with self.assertRaisesRegex(ValueError, 'host_not_allowed'):
            fetch_host_file(file, len(FIXTURE), SHA, [])
        for address in ('127.0.0.1', '::1', '169.254.169.254', '100.64.0.1', '10.0.0.1'):
            with patch('socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))]):
                with self.assertRaisesRegex(ValueError, 'not_public'):
                    allowed_url('https://uploads.example.test/x', ['uploads.example.test'])
        for url in ('http://uploads.example.test/x', 'https://' + 'a:b' + '@' + 'uploads.example.test/x',
                    'https://uploads.example.test:8443/x', 'https://uploads.example.test/x#fragment'):
            with self.assertRaises(ValueError): allowed_url(url, ['uploads.example.test'])

    def test_redirect_and_hash_size_failure_never_return_unverified_bytes(self):
        class Connection:
            status = 200
            def __init__(self, *_): self.reader = io.BytesIO(FIXTURE)
            def request(self, *_args, **_kwargs): pass
            def getresponse(self): return self
            def getheader(self, name, default=None):
                return str(len(FIXTURE)) if name == 'Content-Length' else default
            def read(self, size): return self.reader.read(size)
            def close(self): pass
        file = OpenAIFile(download_url='https://uploads.example.test/signed-secret', file_id='synthetic')
        with patch('telegram_setup.file_transport.allowed_url', return_value=(NS(hostname='uploads.example.test', path='/fixture', query=''), [NS()])), \
                patch('telegram_setup.file_transport.PinnedHTTPS', Connection):
            self.assertEqual(fetch_host_file(file, len(FIXTURE), SHA, []), FIXTURE)
            with self.assertRaisesRegex(ValueError, 'integrity'): fetch_host_file(file, len(FIXTURE), '0' * 64, [])
            with self.assertRaisesRegex(ValueError, 'size'): fetch_host_file(file, len(FIXTURE) - 1, SHA, [])
            Connection.status = 302
            with self.assertRaisesRegex(ValueError, 'redirect'): fetch_host_file(file, len(FIXTURE), SHA, [])
