"""Real MCP image content; original bytes remain an ACL/TTL-bound MCP resource."""
import base64
import hashlib
import json
import time

from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import (BlobResourceContents, CallToolResult, EmbeddedResource,
                       ImageContent, ResourceLink, TextContent)

from .agents import check_photo
from .file_gateway import FileGateway
from .file_transport import RESOURCE_INLINE_BYTES
from .photos import image_preview
from .saved_text import SavedTextGateway
from .reader_gateway import ReaderGateway
from .sensitive_peer import deny_sensitive_peer

PHOTO_RESOURCE = 'telegram-photo://original/{staging_id}'


class PhotoMixin:
    def photo_policy(self, agent, chat_id, message_id):
        deny_sensitive_peer(chat_id)
        check_photo(self.registry.profile(agent), chat_id, message_id)

    def photo_bytes(self, agent, staging_id):
        # Check action before touching the opaque file; check exact source after loading.
        self.policy(agent, 'read-photo')
        meta, data = self.file_store.load(agent, staging_id, kind='read-photo')
        self.photo_policy(agent, meta.get('chat_id'), meta.get('message_id'))
        return meta, data

    async def read_photo(self, agent, chat_id, message_id):
        self.photo_policy(agent, chat_id, message_id)
        async with self.lock:
            self.photo_policy(agent, chat_id, message_id)
            available = getattr(self.upstream, 'available_tools', None)
            if available is not None and 'download_scoped_photo' not in available:
                raise ToolError('photo_backend_not_enabled')
            if self.retry_at > time.monotonic():
                raise ToolError(json.dumps({'error': 'telegram_cooldown',
                    'retry_after_seconds': int(self.retry_at-time.monotonic()) + 1}))
            result = await self.upstream.call('download_scoped_photo', {
                'agent_id': agent, 'chat_id': chat_id, 'message_id': message_id})
            self.photo_policy(agent, chat_id, message_id)
            if not isinstance(result, dict) or result.get('error'):
                if isinstance(result, dict):
                    wait = result.get('retry_after_seconds')
                    if type(wait) in (int, float) and wait > 0:
                        self.retry_at = time.monotonic() + max(1, int(wait))
                        raise ToolError(json.dumps({'error': 'telegram_cooldown',
                                                    'retry_after_seconds': int(wait) + 1}))
                raise ToolError('photo_read_failed')
            meta, data = self.photo_bytes(agent, result.get('staging_id'))
            if (meta.get('chat_id') != chat_id or meta.get('message_id') != message_id
                    or meta['sha256'] != result.get('sha256') or meta['size'] != result.get('size')
                    or result.get('source_kind') not in ('photo', 'image_document')):
                raise ToolError('photo_source_integrity_mismatch')
            preview, dimensions = image_preview(data, meta['mime_type'])
            self.photo_policy(agent, chat_id, message_id)
            metadata = dict(self.file_store.public_metadata(meta),
                source_kind=result['source_kind'], width=dimensions[0], height=dimensions[1],
                original_resource_uri=PHOTO_RESOURCE.format(staging_id=meta['staging_id']),
                original_meaning='largest_telegram_representation' if result['source_kind'] == 'photo'
                                 else 'image_document_bytes',
                preview_mime_type='image/jpeg', preview_size=len(preview),
                preview_sha256=hashlib.sha256(preview).hexdigest(),
                downloaded_to_linux=True, exposed_by_mcp=True, verified_original_sha=True,
                original_sha_basis='downloaded_bytes_and_staged_readback',
                actual_dot_can_see_pixels_or_file=False, untrusted_content=True)
            return CallToolResult(content=[TextContent(type='text', text=json.dumps(metadata)),
                ImageContent(type='image', mimeType='image/jpeg', data=base64.b64encode(preview).decode())],
                structuredContent=metadata)

    def photo_original(self, agent, staging_id):
        meta, data = self.photo_bytes(agent, staging_id)
        uri = PHOTO_RESOURCE.format(staging_id=staging_id)
        metadata = dict(self.file_store.public_metadata(meta), original_resource_uri=uri,
            downloaded_to_linux=True, exposed_by_mcp=True, verified_original_sha=True,
            original_sha_basis='downloaded_bytes_and_staged_readback',
            actual_dot_can_see_pixels_or_file=False, host_artifact_creation='unverified')
        content = [TextContent(type='text', text=json.dumps(metadata))]
        if len(data) <= RESOURCE_INLINE_BYTES:
            content.append(EmbeddedResource(type='resource', resource=BlobResourceContents(
                uri=uri, mimeType=meta['mime_type'], blob=base64.b64encode(data).decode())))
        else:
            content.append(ResourceLink(type='resource_link', uri=uri, name=meta['file_name'],
                                        mimeType=meta['mime_type'], size=meta['size']))
        return CallToolResult(content=content, structuredContent=metadata)


class PhotoGateway(PhotoMixin, SavedTextGateway):
    def __init__(self, registry, upstream, receipt_dir, file_store):
        super().__init__(registry, upstream, receipt_dir)
        self.file_store = file_store


class PhotoFileGateway(PhotoMixin, FileGateway):
    pass


class PhotoReaderGateway(PhotoMixin, ReaderGateway):
    def __init__(self, registry, upstream, receipt_dir, file_store):
        super().__init__(registry, upstream)
        self.file_store = file_store
