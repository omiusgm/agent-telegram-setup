"""Bounded exact-message photo reads using the existing shared Telegram client."""
import asyncio
import base64
import hashlib
import io
import time
import warnings
from typing import Optional

from mcp.types import CallToolResult, ImageContent, TextContent

from .agents import AccessDenied, check_photo
from .file_transport import StagedFiles
from .saved_files import BoundedBytesIO

IMAGE_MIMES = {'JPEG': 'image/jpeg', 'PNG': 'image/png', 'WEBP': 'image/webp'}
MAX_PIXELS = 20_000_000
MAX_SIDE = 12000
PREVIEW_BYTES = 512 * 1024


def image_preview(data, declared_mime):
    # Pillow is already an upstream dependency. Import lazily for readers without media.
    from PIL import Image, ImageOps
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        try:
            with Image.open(io.BytesIO(data)) as image:
                mime = IMAGE_MIMES.get(image.format)
                width, height = image.size
                if (mime != declared_mime or not mime or width * height > MAX_PIXELS
                        or max(width, height) > MAX_SIDE or getattr(image, 'n_frames', 1) != 1):
                    raise ValueError('photo_format_or_dimensions_rejected')
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                preview = ImageOps.exif_transpose(image).convert('RGB')
                preview.thumbnail((1280, 1280))
                while True:
                    target = io.BytesIO()
                    preview.save(target, format='JPEG', quality=82)
                    output = target.getvalue()
                    if len(output) <= PREVIEW_BYTES:
                        return output, (width, height)
                    preview.thumbnail((max(1, preview.width // 2), max(1, preview.height // 2)))
        except (ValueError, OSError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError):
            raise ValueError('photo_invalid_mime_bytes_or_dimensions') from None


class PhotoBackend:
    def __init__(self, client, registry, store):
        self.client, self.registry, self.store = client, registry, store

    def policy(self, agent, chat_id, message_id):
        check_photo(self.registry.profile(agent), chat_id, message_id)

    @staticmethod
    def identity(message, chat_id, message_id):
        if (not message or message.id != message_id or message.chat_id != chat_id
                or getattr(message, 'noforwards', False)):
            raise ValueError('photo_source_missing_private_or_protected')
        photo, doc = getattr(message, 'photo', None), getattr(message, 'document', None)
        if photo:
            mime, source = 'image/jpeg', photo
        elif doc and getattr(doc, 'mime_type', None) in IMAGE_MIMES.values():
            # Stickers/animations are not image-document photos.
            if any(type(a).__name__ in ('DocumentAttributeSticker', 'DocumentAttributeAnimated',
                                       'DocumentAttributeVideo') for a in getattr(doc, 'attributes', [])):
                raise ValueError('photo_document_type_rejected')
            mime, source = doc.mime_type, doc
        else:
            raise ValueError('photo_or_image_document_required')
        file = getattr(message, 'file', None)
        size = getattr(file, 'size', None)
        if type(size) is not int or size <= 0:
            raise ValueError('photo_size_unavailable')
        return (('photo' if photo else 'image_document'), source.id, mime, size)

    async def read(self, agent, chat_id, message_id):
        self.policy(agent, chat_id, message_id)
        message = await asyncio.wait_for(self.client.get_messages(chat_id, ids=message_id), 20)
        self.policy(agent, chat_id, message_id)
        identity = self.identity(message, chat_id, message_id)
        kind, _, mime, size = identity
        if size > self.store.maximum:
            raise ValueError('photo_size_exceeded')
        for attempt in range(2):
            target = BoundedBytesIO(self.store.maximum)
            def progress(received, total):
                if received > size or total > size:
                    raise ValueError('photo_size_exceeded')
            try:
                await asyncio.wait_for(self.client.download_media(
                    message, file=target, progress_callback=progress), 45)
                break
            except Exception as exc:
                if type(exc).__name__ not in ('FileReferenceExpiredError', 'FileReferenceInvalidError') or attempt:
                    raise
                self.policy(agent, chat_id, message_id)
                message = await asyncio.wait_for(self.client.get_messages(chat_id, ids=message_id), 20)
                if self.identity(message, chat_id, message_id) != identity:
                    raise ValueError('photo_source_changed') from None
        data = target.getvalue()
        self.policy(agent, chat_id, message_id)
        if len(data) != size:
            raise ValueError('photo_partial_read')
        image_preview(data, mime)  # Validate decoder/MIME/dimensions before storing.
        # Never publish bytes if the message was deleted/edited during the download.
        current = await asyncio.wait_for(self.client.get_messages(chat_id, ids=message_id), 20)
        if self.identity(current, chat_id, message_id) != identity:
            raise ValueError('photo_source_changed')
        self.policy(agent, chat_id, message_id)
        extension = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp'}[mime]
        meta = self.store.put(agent, data, 'selected-photo.' + extension, mime,
                              kind='read-photo', chat_id=chat_id, message_id=message_id)
        self.policy(agent, chat_id, message_id)
        return dict(meta, source_kind=kind, downloaded_to_linux=True,
                    verified_original_sha=True, actual_dot_can_see_pixels_or_file=False)


def register_photos(runtime):
    from .config import private_dir
    from .agents import AgentRegistry
    registry = AgentRegistry(private_dir() / 'agents.json')
    store = StagedFiles(private_dir() / 'files' / 'agent-staging')
    lock, cooldown = asyncio.Lock(), {}

    @runtime.mcp.tool(annotations=runtime.ToolAnnotations(
        title='Read an exact permitted photo', readOnlyHint=True, openWorldHint=True))
    @runtime.with_account(readonly=True)
    async def download_scoped_photo(agent_id: str, chat_id: int, message_id: int,
                                     account: Optional[str] = None) -> dict:
        """Private shared backend. No paths, URLs, avatars, sibling messages or sends."""
        async with lock:
            api = PhotoBackend(runtime.get_client(account), registry, store)
            api.policy(agent_id, chat_id, message_id)
            key = account or ''
            remaining = cooldown.get(key, 0) - time.monotonic()
            if remaining > 0:
                return {'error': 'telegram_cooldown', 'retry_after_seconds': int(remaining) + 1}
            try:
                return await asyncio.wait_for(api.read(agent_id, chat_id, message_id), 90)
            except AccessDenied:
                return {'error': 'photo_access_denied'}
            except Exception as exc:
                seconds = getattr(exc, 'seconds', None)
                if type(seconds) is int and 0 < seconds <= 86400 * 30:
                    cooldown[key] = time.monotonic() + seconds
                    return {'error': 'telegram_cooldown', 'retry_after_seconds': seconds}
                # No Telegram exception text, file references, account IDs or paths.
                return {'error': 'photo_read_failed', 'failure_category':
                        'timeout' if isinstance(exc, TimeoutError) else 'source_or_media_unavailable'}


def synthetic_image_probe():
    """Fixed synthetic pixels, no Telegram reads. Landmarks are not described in text."""
    from PIL import Image, ImageDraw
    image = Image.new('RGB', (480, 320), 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((30, 30, 150, 150), fill='#16a34a')
    draw.ellipse((290, 40, 410, 160), fill='#dc2626')
    draw.polygon([(240, 190), (180, 290), (300, 290)], fill='#2563eb')
    output = io.BytesIO(); image.save(output, 'PNG')
    data = output.getvalue()
    meta = {'synthetic': True, 'telegram_calls': 0, 'mime_type': 'image/png',
            'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'exposed_by_mcp': True, 'actual_dot_can_see_pixels_or_file': False}
    import json
    return CallToolResult(content=[TextContent(type='text', text=json.dumps(meta)),
        ImageContent(type='image', mimeType='image/png', data=base64.b64encode(data).decode())],
        structuredContent=meta)
