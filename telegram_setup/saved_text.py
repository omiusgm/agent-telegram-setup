"""Opt-in MVP write: one plain text to Saved Messages with a durable receipt."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

from .agents import SAVED_TEXT_ACTION
from .reader_gateway import ReaderGateway


class SendReceipts:
    """Claim before dispatch, so a lost response never causes a blind resend."""
    def __init__(self, folder):
        self.folder = Path(folder)

    def path_for(self, agent, request_id):
        key = hashlib.sha256((agent + '\0' + request_id).encode()).hexdigest()
        return self.folder / (key + '.json')

    def existing(self, agent, request_id, fingerprint):
        try:
            info = self.folder.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError('unsafe_send_receipt_directory')
        try:
            return self.read(self.path_for(agent, request_id), fingerprint)
        except FileNotFoundError:
            return None

    def read(self, path, fingerprint):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 16384:
                raise RuntimeError('unsafe_send_receipt')
            old = json.load(stream)
        if not isinstance(old, dict) or old.get('fingerprint') != fingerprint:
            raise ValueError('request_id_reused_with_different_text')
        return old

    def begin(self, agent, request_id, fingerprint):
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.folder.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError('unsafe_send_receipt_directory')
        path = self.path_for(agent, request_id)
        value = {'fingerprint': fingerprint, 'status': 'uncertain_do_not_retry',
                 'request_id': request_id, 'destination': 'saved_messages', 'readback_verified': False}
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            return path, self.read(path, fingerprint)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush(); os.fsync(stream.fileno())
        self.sync_folder()
        return path, None

    def finish(self, path, fingerprint, receipt):
        fd, temporary = tempfile.mkstemp(prefix='.receipt-', dir=self.folder)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(dict(receipt, fingerprint=fingerprint), stream)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
            self.sync_folder()
        finally:
            Path(temporary).unlink(missing_ok=True)

    def sync_folder(self):
        fd = os.open(self.folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class SavedTextGateway(ReaderGateway):
    def __init__(self, registry, upstream, receipt_dir):
        super().__init__(registry, upstream)
        self.receipts = SendReceipts(receipt_dir)

    def remember_cooldown(self, result, receipt):
        wait = result.get('retry_after_seconds') if isinstance(result, dict) else None
        if type(wait) in (int, float) and wait > 0:
            wait = max(1, int(wait))
            self.retry_at = time.monotonic() + wait
            receipt['retry_after_seconds'] = wait

    async def status(self, agent):
        result = await super().status(agent)
        permitted = SAVED_TEXT_ACTION in self.registry.profile(agent).get('actions', [])
        ready = bool(getattr(self.upstream, 'send_text_available', False))
        result.update(read_only=not (permitted and ready), send_saved_text_allowed=permitted,
                      send_text_backend_ready=ready)
        return result

    async def send_saved_text(self, agent, text, request_id):
        self.policy(agent, SAVED_TEXT_ACTION)
        if not isinstance(text, str) or not text.strip() or len(text.encode('utf-16-le')) // 2 > 4096:
            raise ValueError('text_must_be_1_to_4096_utf16_units')
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{8,128}', request_id):
            raise ValueError('request_id_required')
        async with self.lock:
            self.policy(agent, SAVED_TEXT_ACTION)  # Also checks queued revocation.
            fingerprint = hashlib.sha256(text.encode()).hexdigest()
            previous = self.receipts.existing(agent, request_id, fingerprint)
            if previous is not None:
                return {key: value for key, value in previous.items() if key != 'fingerprint'}
            if not getattr(self.upstream, 'send_text_available', False):
                return {'error': 'send_text_backend_not_enabled', 'sent': False}
            if self.retry_at > time.monotonic():
                return {'error': 'telegram_cooldown', 'sent': False,
                        'retry_after_seconds': int(self.retry_at - time.monotonic()) + 1}
            path, previous = self.receipts.begin(agent, request_id, fingerprint)
            if previous is not None:
                return {key: value for key, value in previous.items() if key != 'fingerprint'}
            receipt = {'request_id': request_id, 'destination': 'saved_messages',
                       'status': 'uncertain_do_not_retry', 'readback_verified': False}
            # Metadata-only identity check uses the existing shared MCP.
            # No new Telegram client or session is created here.
            try:
                owner = await self.upstream.call('get_me', {})
                self.remember_cooldown(owner, receipt)
                if (not isinstance(owner, dict) or owner.get('type') != 'user'
                        or type(owner.get('id')) is not int or owner['id'] <= 0):
                    self.receipts.finish(path, fingerprint, receipt)
                    return receipt
                self.policy(agent, SAVED_TEXT_ACTION)
                reply = await self.upstream.call('send_message',
                    {'chat_id': 'me', 'message': text, 'parse_mode': None})
                self.remember_cooldown(reply, receipt)
                match = re.fullmatch(r'Message sent successfully\. message_id=([1-9][0-9]*)',
                                     reply.strip()) if isinstance(reply, str) else None
                if match:
                    message_id = int(match.group(1))
                    receipt['message_id'] = message_id
                    saved = await self.upstream.call('read_history_page',
                        {'chat_id': owner['id'], 'after_id': message_id-1,
                         'until_id': message_id, 'limit': 1})
                    self.remember_cooldown(saved, receipt)
                    messages = saved.get('messages', []) if isinstance(saved, dict) else []
                    if (isinstance(saved, dict) and saved.get('chat_id') == owner['id']
                            and len(messages) == 1 and messages[0].get('id') == message_id
                            and messages[0].get('text') == text):
                        receipt.update(status='delivered_verified', readback_verified=True)
            except Exception:
                # It may already be in Telegram; preserve the claim on all failures.
                pass
            self.receipts.finish(path, fingerprint, receipt)
            return receipt
