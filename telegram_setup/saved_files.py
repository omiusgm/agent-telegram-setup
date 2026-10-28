"""Fixed Saved Messages file tools, within the one existing account backend."""
import asyncio
import hashlib
import io
import json
from pathlib import Path
import re
from typing import Optional

from .agents import AgentRegistry, check_action
from .file_transport import MAX_FILE_BYTES, StagedFiles, validate_file_metadata
from .saved_text import SendReceipts

SEND_FILE_ACTION = 'send-saved-file'
DOWNLOAD_FILE_ACTION = 'download-saved-file'


def request_id_valid(request_id):
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{8,128}', request_id):
        raise ValueError('request_id_required')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class BoundedBytesIO(io.BytesIO):
    def __init__(self, maximum):
        super().__init__(); self.maximum = maximum

    def write(self, data):
        if self.tell() + len(data) > self.maximum:
            raise ValueError('saved_attachment_size_exceeded')
        return super().write(data)


class SavedFileBackend:
    def __init__(self, client, registry, store, receipts, *, self_peer):
        self.client, self.registry, self.store, self.receipts = client, registry, store, receipts
        self.self_peer = self_peer

    def policy(self, agent, action, owner=None):
        check_action(self.registry.profile(agent), action, owner)

    async def owner(self, agent, action):
        self.policy(agent, action)
        me = await self.client.get_me()
        if not me or getattr(me, 'bot', False) or type(me.id) is not int or me.id <= 0:
            raise ValueError('saved_owner_identity_unavailable')
        self.policy(agent, action, me.id)
        return me.id

    async def download_bytes(self, message):
        file = getattr(message, 'file', None)
        if not file or type(file.size) is not int or not 1 <= file.size <= self.store.maximum:
            raise ValueError('saved_attachment_size_unavailable_or_exceeded')
        target = BoundedBytesIO(self.store.maximum)
        def progress(received, total):
            if received > self.store.maximum or total > self.store.maximum:
                raise ValueError('saved_attachment_size_exceeded')
        await self.client.download_media(message, file=target, progress_callback=progress)
        data = target.getvalue()
        if len(data) != file.size or len(data) > self.store.maximum:
            raise ValueError('saved_attachment_size_mismatch')
        return data

    @staticmethod
    def verify_message(message, message_id, owner):
        if (not message or message.id != message_id or message.chat_id != owner
                or not getattr(message, 'document', None)):
            raise ValueError('saved_document_identity_mismatch')

    async def send(self, agent, staging_id, request_id, file_name, mime_type, size, sha256, caption=''):
        self.policy(agent, SEND_FILE_ACTION)
        request_id_valid(request_id)
        validate_file_metadata(file_name, mime_type, size, sha256)
        if not isinstance(caption, str) or len(caption.encode('utf-16-le')) // 2 > 1024:
            raise ValueError('plain_caption_too_long')
        key = agent + ':send-file'
        digest = fingerprint([staging_id, file_name, mime_type, size, sha256, caption, 'me'])
        old = self.receipts.existing(key, request_id, digest)
        if old is not None:
            return {k: v for k, v in old.items() if k != 'fingerprint'}
        meta, data = self.store.load(agent, staging_id, kind='upload')
        if any(meta[k] != v for k, v in {'file_name': file_name, 'mime_type': mime_type,
                                        'size': size, 'sha256': sha256}.items()):
            raise ValueError('staged_file_metadata_mismatch')
        owner = await self.owner(agent, SEND_FILE_ACTION)
        path, old = self.receipts.begin(key, request_id, digest)
        if old is not None:
            return {k: v for k, v in old.items() if k != 'fingerprint'}
        receipt = {'request_id': request_id, 'destination': 'saved_messages',
                   'status': 'uncertain_do_not_retry', 'readback_verified': False,
                   'sha256': sha256, 'size': size, 'file_name': file_name, 'mime_type': mime_type}
        try:
            self.policy(agent, SEND_FILE_ACTION, owner)
            source = io.BytesIO(data); source.name = file_name
            sent = await self.client.send_file(self.self_peer, source, caption=caption,
                                                force_document=True, parse_mode=None, mime_type=mime_type)
            if not sent or type(sent.id) is not int or sent.id <= 0:
                raise ValueError('sent_message_id_unavailable')
            receipt['message_id'] = sent.id
            self.policy(agent, SEND_FILE_ACTION, owner)
            message = await self.client.get_messages(self.self_peer, ids=sent.id)
            self.verify_message(message, sent.id, owner)
            returned = await self.download_bytes(message)
            self.policy(agent, SEND_FILE_ACTION, owner)
            if (len(returned) != size or hashlib.sha256(returned).hexdigest() != sha256
                    or message.file.name != file_name or message.file.mime_type != mime_type
                    or (getattr(message, 'message', None) or '') != caption):
                raise ValueError('saved_download_back_mismatch')
            staged = self.store.put(agent, returned, file_name, mime_type, kind='download', message_id=sent.id)
            receipt.update(status='delivered_verified', readback_verified=True,
                           download_staging_id=staged['staging_id'], expires_at=staged['expires_at'])
        except Exception as exc:
            seconds = getattr(exc, 'seconds', None)
            if type(seconds) is int and seconds > 0:
                receipt['retry_after_seconds'] = seconds
            # A send may already have happened. Never retry automatically.
        self.receipts.finish(path, digest, receipt)
        self.policy(agent, SEND_FILE_ACTION, owner)
        return receipt

    async def download(self, agent, message_id):
        if type(message_id) is not int or message_id <= 0:
            raise ValueError('positive_message_id_required')
        owner = await self.owner(agent, DOWNLOAD_FILE_ACTION)
        message = await self.client.get_messages(self.self_peer, ids=message_id)
        self.verify_message(message, message_id, owner)
        data = await self.download_bytes(message)
        self.policy(agent, DOWNLOAD_FILE_ACTION, owner)
        staged = self.store.put(agent, data, message.file.name or 'attachment.bin',
                                message.file.mime_type or 'application/octet-stream',
                                kind='download', message_id=message_id)
        return dict(staged, destination='saved_messages', telegram_download_verified=True,
                    dot_file_received_verified=False)


def register_saved_files(runtime):
    """Registration creates no session/client and makes no network request."""
    from .config import private_dir
    from telethon.tl.types import InputPeerSelf
    store = StagedFiles(private_dir() / 'files' / 'agent-staging')
    registry = AgentRegistry(private_dir() / 'agents.json')
    receipts = SendReceipts(private_dir() / 'file-send-receipts')
    lock = asyncio.Lock()

    @runtime.mcp.tool(annotations=runtime.ToolAnnotations(
        title='Send staged file to Saved Messages', readOnlyHint=False, openWorldHint=True))
    @runtime.with_account(readonly=False)
    async def send_staged_saved_file(agent_id: str, staging_id: str, request_id: str,
                                     file_name: str, mime_type: str, size: int, sha256: str,
                                     caption: str = '', account: Optional[str] = None) -> dict:
        """Private backend: opaque staged file only, fixed receiver InputPeerSelf."""
        async with lock:
            api = SavedFileBackend(runtime.get_client(account), registry, store, receipts,
                                   self_peer=InputPeerSelf())
            return await api.send(agent_id, staging_id, request_id, file_name, mime_type, size, sha256, caption)

    @runtime.mcp.tool(annotations=runtime.ToolAnnotations(
        title='Download Saved document into scoped staging', readOnlyHint=True, openWorldHint=True))
    @runtime.with_account(readonly=True)
    async def download_saved_attachment(agent_id: str, message_id: int,
                                         account: Optional[str] = None) -> dict:
        """Only Saved Messages documents; returns metadata, never a caller-selected path."""
        async with lock:
            api = SavedFileBackend(runtime.get_client(account), registry, store, receipts,
                                   self_peer=InputPeerSelf())
            return await api.download(agent_id, message_id)
