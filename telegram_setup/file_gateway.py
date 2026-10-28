"""Agent-facing host-file staging, Saved send/download and standard MCP resources."""
import asyncio
import base64
import json
import hashlib
from pathlib import Path
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict
from mcp.server.fastmcp.exceptions import ToolError

from mcp.types import BlobResourceContents, CallToolResult, EmbeddedResource, ResourceLink, TextContent

from .file_transport import RESOURCE_INLINE_BYTES, StagedFiles, fetch_host_file
from .saved_files import SEND_FILE_ACTION, DOWNLOAD_FILE_ACTION, fingerprint, request_id_valid
from .saved_text import SavedTextGateway, SendReceipts

# This is an actually registered MCP resource template, not a ChatGPT file URL/ID.
RESOURCE_TEMPLATE = 'telegram-saved://staging/{staging_id}'


class FileResourceMetadata(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    staging_id: str
    kind: Literal['download']
    file_name: str
    mime_type: str
    size: int
    sha256: str
    expires_at: int
    message_id: int
    resource_uri: str
    telegram_download_verified: Literal[True]
    dot_file_received_verified: Literal[False]
    output_transport: Literal['standard_mcp_resource']
    host_artifact_creation: Literal['unverified']


class FileGateway(SavedTextGateway):
    def __init__(self, registry, upstream, receipt_dir, file_store, *, file_hosts=(), fetcher=fetch_host_file):
        super().__init__(registry, upstream, receipt_dir)
        self.file_store, self.file_hosts, self.fetcher = file_store, frozenset(file_hosts), fetcher
        self.file_receipts = SendReceipts(Path(receipt_dir) / 'file-gateway')

    async def status(self, agent):
        result = await super().status(agent)
        actions = self.registry.profile(agent).get('actions', [])
        result.update(send_saved_file_allowed=SEND_FILE_ACTION in actions,
                      download_saved_file_allowed=DOWNLOAD_FILE_ACTION in actions,
                      saved_files_backend_ready=bool(getattr(self.upstream, 'saved_files_available', False)),
                      file_max_bytes=self.file_store.maximum, file_ttl_seconds=self.file_store.ttl,
                      native_input_host_count=len(self.file_hosts), dot_file_received_verified=False)
        if SEND_FILE_ACTION in actions and getattr(self.upstream, 'saved_files_available', False):
            result['read_only'] = False
        return result

    async def stage_file(self, agent, file, expected_size, expected_sha256):
        self.policy(agent, SEND_FILE_ACTION)
        # Fetcher validates schema, host, pinned public DNS/TLS, byte limit and hash.
        data = await asyncio.to_thread(self.fetcher, file, expected_size, expected_sha256, self.file_hosts)
        if len(data) != expected_size or hashlib.sha256(data).hexdigest() != expected_sha256:
            raise ValueError('host_file_integrity_failed')
        self.policy(agent, SEND_FILE_ACTION)
        staged = self.file_store.put(agent, data, file.file_name or 'attachment.bin',
                                      file.mime_type or 'application/octet-stream')
        return dict(staged, host_input_download_verified=True, telegram_sent=False)

    def stage_text_file(self, agent, text, file_name):
        self.policy(agent, SEND_FILE_ACTION)
        if not isinstance(text, str) or not 1 <= len(text.encode()) <= RESOURCE_INLINE_BYTES:
            raise ValueError('generated_text_file_must_be_1_to_65536_utf8_bytes')
        if not isinstance(file_name, str) or not file_name.endswith(('.txt', '.md', '.csv', '.json')):
            raise ValueError('generated_text_file_extension_required')
        staged = self.file_store.put(agent, text.encode(), file_name, 'text/plain')
        return dict(staged, input_transport='generated_utf8_text', native_host_input_verified=False,
                    telegram_sent=False)

    async def send_saved_file(self, agent, staging_id, request_id, caption=''):
        self.policy(agent, SEND_FILE_ACTION)
        request_id_valid(request_id)
        if not isinstance(caption, str) or len(caption.encode('utf-16-le')) // 2 > 1024:
            raise ValueError('plain_caption_too_long')
        digest = fingerprint([staging_id, caption, 'me'])
        async with self.lock:
            self.policy(agent, SEND_FILE_ACTION)
            old = self.file_receipts.existing(agent, request_id, digest)
            if old is not None:
                return {k: v for k, v in old.items() if k != 'fingerprint'}
            meta, _ = self.file_store.load(agent, staging_id, kind='upload')
            if not getattr(self.upstream, 'saved_files_available', False):
                return {'error': 'saved_files_backend_not_enabled', 'sent': False}
            if self.retry_at > time.monotonic():
                return {'error': 'telegram_cooldown', 'sent': False,
                        'retry_after_seconds': int(self.retry_at - time.monotonic()) + 1}
            path, old = self.file_receipts.begin(agent, request_id, digest)
            if old is not None:
                return {k: v for k, v in old.items() if k != 'fingerprint'}
            receipt = {'request_id': request_id, 'destination': 'saved_messages',
                       'status': 'uncertain_do_not_retry', 'readback_verified': False}
            try:
                result = await self.upstream.call('send_staged_saved_file', {
                    'agent_id': agent, 'staging_id': staging_id, 'request_id': request_id,
                    'caption': caption, **{k: meta[k] for k in ('file_name', 'mime_type', 'size', 'sha256')}})
                self.remember_cooldown(result, receipt)
                if (isinstance(result, dict) and result.get('destination') == 'saved_messages'
                        and result.get('request_id') == request_id
                        and result.get('sha256') == meta['sha256'] and result.get('size') == meta['size']):
                    receipt = {k: v for k, v in result.items() if k in {
                        'request_id', 'destination', 'status', 'readback_verified', 'message_id',
                        'sha256', 'size', 'file_name', 'mime_type', 'download_staging_id', 'expires_at',
                        'retry_after_seconds'}}
                    if (receipt.get('status') == 'delivered_verified' and
                            (receipt.get('readback_verified') is not True or type(receipt.get('message_id')) is not int)):
                        receipt.update(status='uncertain_do_not_retry', readback_verified=False)
            except Exception:
                pass
            self.file_receipts.finish(path, digest, receipt)
            self.policy(agent, SEND_FILE_ACTION)
            return receipt

    async def download_saved_file(self, agent, message_id):
        self.policy(agent, DOWNLOAD_FILE_ACTION)
        if type(message_id) is not int or message_id <= 0:
            raise ValueError('positive_message_id_required')
        async with self.lock:
            self.policy(agent, DOWNLOAD_FILE_ACTION)
            if not getattr(self.upstream, 'saved_files_available', False):
                raise ToolError('saved_files_backend_not_enabled')
            if self.retry_at > time.monotonic():
                raise ToolError(json.dumps({'error': 'telegram_cooldown',
                                           'retry_after_seconds': int(self.retry_at-time.monotonic())+1}))
            result = await self.upstream.call('download_saved_attachment', {'agent_id': agent, 'message_id': message_id})
            self.policy(agent, DOWNLOAD_FILE_ACTION)
            self.remember_cooldown(result, {})
            if not isinstance(result, dict) or result.get('error'):
                error = {'error': 'telegram_cooldown', 'retry_after_seconds': result['retry_after_seconds']} if (
                    isinstance(result, dict) and result.get('retry_after_seconds')) else {'error': 'saved_download_failed'}
                raise ToolError(json.dumps(error))
            meta, _ = self.file_store.load(agent, result.get('staging_id'), kind='download')
            if meta.get('message_id') != message_id or meta['sha256'] != result.get('sha256'):
                raise RuntimeError('saved_download_identity_mismatch')
            return self.resource_result(agent, meta['staging_id'])

    def resource_bytes(self, agent, staging_id):
        self.policy(agent, DOWNLOAD_FILE_ACTION)
        meta, data = self.file_store.load(agent, staging_id, kind='download')
        self.policy(agent, DOWNLOAD_FILE_ACTION)
        return meta, data

    def resource_result(self, agent, staging_id):
        meta, data = self.resource_bytes(agent, staging_id)
        uri = RESOURCE_TEMPLATE.format(staging_id=staging_id)
        result = dict(self.file_store.public_metadata(meta), resource_uri=uri,
                      telegram_download_verified=True, dot_file_received_verified=False,
                      output_transport='standard_mcp_resource', host_artifact_creation='unverified')
        contents = [TextContent(type='text', text=json.dumps(result))]
        if len(data) <= RESOURCE_INLINE_BYTES:
            contents.append(EmbeddedResource(type='resource', resource=BlobResourceContents(
                uri=uri, mimeType=meta['mime_type'], blob=base64.b64encode(data).decode())))
        else:
            contents.append(ResourceLink(type='resource_link', uri=uri, name=meta['file_name'],
                                         mimeType=meta['mime_type'], size=meta['size']))
        return CallToolResult(content=contents, structuredContent=result)
