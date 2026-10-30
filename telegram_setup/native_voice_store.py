"""Private durable claims and results for Telegram-native voice transcription.

The cache contains transcript text and chat/message IDs. It is owner-only local
state, not a log or a defence against processes running as the same Unix UID.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from .poll_common import valid_request_id
from .private_state import secure_directory, write_json

MAX_RECORD_BYTES = 300000
MAX_RECORDS = 5000


def valid_transcription_id(value):
    # Telegram declares TL long, not an always-positive ID.
    return type(value) is int and -(2**63) <= value < 2**63


def _hash(*values):
    return hashlib.sha256(json.dumps(values, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _unique_pairs(pairs):
    if len({key for key, _ in pairs}) != len(pairs): raise ValueError('invalid_voice_cache')
    return dict(pairs)


class NativeVoiceStore:
    def __init__(self, root): self.root = Path(root)

    @contextmanager
    def locked(self):
        root = secure_directory(self.root)
        for name in ('voices', 'requests', 'cooldowns'): secure_directory(root / name)
        fd = os.open(root / '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600): raise PermissionError('voice_cache_lock_unsafe')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    @staticmethod
    def voice_key(owner_id, chat_id, message_id): return _hash('voice-v1', owner_id, chat_id, message_id)

    @staticmethod
    def request_key(agent, request_id):
        valid_request_id(request_id)
        if not isinstance(agent, str) or not agent or len(agent) > 64: raise ValueError('invalid_agent')
        return _hash('voice-request-v1', agent, request_id)

    @staticmethod
    def _read(path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MAX_RECORD_BYTES):
                raise PermissionError('voice_cache_file_unsafe')
            data = stream.read(MAX_RECORD_BYTES + 1)
        if len(data) > MAX_RECORD_BYTES: raise ValueError('invalid_voice_cache')
        value = json.loads(data, object_pairs_hook=_unique_pairs)
        if not isinstance(value, dict): raise ValueError('invalid_voice_cache')
        return value

    @staticmethod
    def _new(path, value):
        folder = secure_directory(path.parent)
        if len(list(folder.glob('*.json'))) >= MAX_RECORDS: raise ValueError('voice_cache_capacity_reached')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                fd = None
                json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
                stream.flush(); os.fsync(stream.fileno())
            directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try: os.fsync(directory)
            finally: os.close(directory)
        finally:
            if fd is not None: os.close(fd)

    def _voice(self, key, chat_id, message_id):
        value = self._read(self.root / 'voices' / (key + '.json'))
        if (type(value.get('version')) is not int or value['version'] != 1 or value.get('voice_key') != key
                or type(value.get('chat_id')) is not int or type(value.get('message_id')) is not int
                or value.get('chat_id') != chat_id or value.get('message_id') != message_id):
            raise ValueError('invalid_voice_cache')
        if (value.get('status') not in ('uncertain_do_not_retry', 'pending', 'complete', 'flood_wait', 'rejected', 'result_too_large')
                or not isinstance(value.get('text'), str)
                or len(value['text'].encode('utf-8')) > 262144
                or not isinstance(value.get('origin_request_key'), str)
                or not re.fullmatch(r'[a-f0-9]{64}', value['origin_request_key'])
                or (value['status'] == 'flood_wait' and type(value.get('retry_at')) is not int)
                or (value['status'] == 'rejected' and value.get('reason') not in (
                    'not_voice', 'voice_too_long', 'telegram_transcription_failed',
                    'premium_or_quota_required', 'message_not_found', 'peer_unavailable'))
                or (value['status'] in ('pending', 'complete')
                    and not valid_transcription_id(value.get('transcription_id')))):
            raise ValueError('invalid_voice_cache')
        if ('trial_remains_num' in value and (type(value['trial_remains_num']) is not int
                                             or value['trial_remains_num'] < 0)):
            raise ValueError('invalid_voice_cache')
        if ('trial_reset_utc' in value and (not isinstance(value['trial_reset_utc'], str)
                                           or len(value['trial_reset_utc']) > 64)):
            raise ValueError('invalid_voice_cache')
        return value

    def get(self, owner_id, chat_id, message_id):
        key = self.voice_key(owner_id, chat_id, message_id)
        with self.locked():
            try: return self._voice(key, chat_id, message_id)
            except FileNotFoundError: return None

    def lookup(self, owner_id, agent, chat_id, message_id, request_id):
        """Check idempotency and reuse cached work without calling Telegram."""
        key = self.voice_key(owner_id, chat_id, message_id)
        req_key = self.request_key(agent, request_id)
        request_path = self.root / 'requests' / (req_key + '.json')
        with self.locked():
            try: request = self._read(request_path)
            except FileNotFoundError: request = None
            if request is not None and (type(request.get('version')) is not int or request['version'] != 1
                                        or request.get('voice_key') != key):
                raise ValueError('request_id_reused_for_different_voice')
            try: voice = self._voice(key, chat_id, message_id)
            except FileNotFoundError:
                if request is not None: raise ValueError('voice_cache_missing_for_request')
                return None, False
            if request is None:
                self._new(request_path, {'version': 1, 'voice_key': key})
            retry = (voice.get('status') == 'flood_wait' and voice.get('origin_request_key') == req_key
                     and type(voice.get('retry_at')) is int and voice['retry_at'] <= int(time.time()))
            return voice, retry

    def claim(self, owner_id, agent, chat_id, message_id, request_id):
        """Claim before RPC; an interrupted claim always prevents blind resend."""
        key = self.voice_key(owner_id, chat_id, message_id)
        req_key = self.request_key(agent, request_id)
        request_path = self.root / 'requests' / (req_key + '.json')
        voice_path = self.root / 'voices' / (key + '.json')
        with self.locked():
            try: request = self._read(request_path)
            except FileNotFoundError: request = None
            if request is not None and (type(request.get('version')) is not int or request['version'] != 1
                                        or request.get('voice_key') != key):
                raise ValueError('request_id_reused_for_different_voice')
            try: voice = self._voice(key, chat_id, message_id)
            except FileNotFoundError: voice = None
            if request is not None and voice is None:
                raise ValueError('voice_cache_missing_for_request')
            if voice is None:
                voice = {'version': 1, 'voice_key': key, 'chat_id': chat_id, 'message_id': message_id,
                         'origin_request_key': req_key, 'status': 'uncertain_do_not_retry',
                         'transcription_id': None, 'text': '', 'created_at': int(time.time())}
                self._new(voice_path, voice)
                dispatch = True
            else:
                dispatch = False
                # A server-declared FloodWait means the RPC was rejected. The
                # same request may be explicitly retried only after the wait.
                if (voice.get('status') == 'flood_wait' and voice.get('origin_request_key') == req_key
                        and type(voice.get('retry_at')) is int and voice['retry_at'] <= int(time.time())):
                    voice.update(status='uncertain_do_not_retry', retry_at=None)
                    write_json(voice_path, voice); dispatch = True
            if request is None:
                self._new(request_path, {'version': 1, 'voice_key': key})
            return voice, dispatch

    def update(self, owner_id, chat_id, message_id, values):
        key = self.voice_key(owner_id, chat_id, message_id)
        with self.locked():
            voice = self._voice(key, chat_id, message_id)
            voice.update(values)
            write_json(self.root / 'voices' / (key + '.json'), voice)
            return voice

    def apply_update(self, owner_id, chat_id, message_id, transcription_id, text, pending, *, oversized=False):
        key = self.voice_key(owner_id, chat_id, message_id)
        with self.locked():
            try: voice = self._voice(key, chat_id, message_id)
            except FileNotFoundError: return False
            if (voice.get('status') != 'pending' or voice.get('transcription_id') != transcription_id):
                return False
            voice.update(status='result_too_large' if oversized else ('pending' if pending else 'complete'),
                         text='' if oversized else text)
            write_json(self.root / 'voices' / (key + '.json'), voice)
            return True

    def cooldown_remaining(self, owner_id):
        path = self.root / 'cooldowns' / (_hash('voice-cooldown-v1', owner_id) + '.json')
        with self.locked():
            try: value = self._read(path)
            except FileNotFoundError: return 0
            until = value.get('retry_at')
            if type(until) is not int: raise ValueError('invalid_voice_cache')
            return max(0, until - int(time.time()))

    def set_cooldown(self, owner_id, seconds):
        if type(seconds) is not int or seconds <= 0: raise ValueError('invalid_voice_cooldown')
        path = self.root / 'cooldowns' / (_hash('voice-cooldown-v1', owner_id) + '.json')
        with self.locked():
            write_json(path, {'retry_at': int(time.time()) + seconds})

    def recent_pending(self, owner_id, max_age=180):
        """Bounded startup scan; older pending claims remain pending on disk."""
        if not self.root.is_dir(): return []
        output, now = [], int(time.time())
        with self.locked():
            paths = list((self.root / 'voices').glob('*.json'))
            if len(paths) > MAX_RECORDS: raise ValueError('voice_cache_capacity_reached')
            for path in paths:
                try:
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_mtime < now - max_age:
                        continue
                    row = self._read(path)
                    chat_id, message_id, since = row.get('chat_id'), row.get('message_id'), row.get('pending_at')
                    if (type(chat_id) is not int or type(message_id) is not int or type(since) is not int
                            or row.get('status') != 'pending' or not 0 <= now - since < max_age
                            or path.stem != self.voice_key(owner_id, chat_id, message_id)):
                        continue
                    self._voice(path.stem, chat_id, message_id)
                    output.append((chat_id, message_id, max_age - (now - since)))
                except (OSError, ValueError, PermissionError):
                    continue
        return output
