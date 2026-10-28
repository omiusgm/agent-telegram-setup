"""Private staging and bounded host-file fetch; no cloud file IDs are fabricated."""
import hashlib
import fcntl
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import time
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, StrictStr

from .private_state import opaque_id, read_private, secure_directory, write_json

MAX_FILE_BYTES = 5 * 1024 * 1024
RESOURCE_INLINE_BYTES = 64 * 1024
FILE_TTL_SECONDS = 900


class OpenAIFile(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True)
    download_url: StrictStr
    file_id: StrictStr
    mime_type: StrictStr = ''
    file_name: StrictStr = ''


def validate_file_metadata(filename, mime_type, size, sha256, maximum=MAX_FILE_BYTES):
    if (not isinstance(filename, str) or not 1 <= len(filename) <= 120
            or filename in ('.', '..') or any(c in filename for c in '/\\\x00\r\n')
            or any(ord(c) < 32 for c in filename)):
        raise ValueError('plain_file_name_required')
    if not isinstance(mime_type, str) or not re.fullmatch(r'[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+', mime_type):
        raise ValueError('invalid_mime_type')
    if type(size) is not int or not 1 <= size <= maximum:
        raise ValueError('file_size_out_of_bounds')
    if not isinstance(sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', sha256):
        raise ValueError('sha256_required')


class StagedFiles:
    def __init__(self, folder, *, maximum=MAX_FILE_BYTES, ttl=FILE_TTL_SECONDS):
        self.folder, self.maximum, self.ttl = Path(folder), maximum, ttl

    def entry(self, staging_id):
        if not isinstance(staging_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32}', staging_id):
            raise ValueError('opaque_staging_id_required')
        secure_directory(self.folder)
        return self.folder / staging_id

    def put(self, agent, data, filename, mime_type, *, kind='upload', message_id=None, chat_id=None,
            peer_id=None, photo_id=None):
        if not isinstance(data, bytes):
            raise ValueError('file_bytes_required')
        validate_file_metadata(filename, mime_type, len(data), hashlib.sha256(data).hexdigest(), self.maximum)
        secure_directory(self.folder)
        fd = os.open(self.folder / '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            return self._put(agent, data, filename, mime_type, kind=kind, message_id=message_id, chat_id=chat_id,
                             peer_id=peer_id, photo_id=photo_id)

    def _put(self, agent, data, filename, mime_type, *, kind='upload', message_id=None, chat_id=None,
             peer_id=None, photo_id=None):
        if not isinstance(data, bytes):
            raise ValueError('file_bytes_required')
        digest = hashlib.sha256(data).hexdigest()
        validate_file_metadata(filename, mime_type, len(data), digest, self.maximum)
        secure_directory(self.folder)
        active, total = 0, 0
        for path in self.folder.iterdir():
            if not re.fullmatch(r'[A-Za-z0-9_-]{32}', path.name):
                continue
            secure_directory(path)
            old = json.loads(read_private(path / 'metadata.json', 8192))
            if old.get('expires_at', 0) <= time.time():
                shutil.rmtree(path)  # Only expired staging, never Telegram source messages.
            elif old.get('agent') == agent:
                active += 1; total += old['size']
        if active >= 8 or total + len(data) > 8 * self.maximum:
            raise ValueError('agent_staging_capacity_reached')
        staging_id = opaque_id()
        directory = self.entry(staging_id)
        directory.mkdir(mode=0o700)
        meta = {'staging_id': staging_id, 'agent': agent, 'kind': kind,
                'file_name': filename, 'mime_type': mime_type, 'size': len(data),
                'sha256': digest, 'expires_at': int(time.time()) + self.ttl}
        if message_id is not None:
            meta['message_id'] = message_id
        if chat_id is not None:
            if type(chat_id) is not int or chat_id == 0 or type(message_id) is not int or message_id <= 0:
                directory.rmdir()
                raise ValueError('numeric_photo_source_required')
            meta['chat_id'] = chat_id
        if peer_id is not None or photo_id is not None:
            if type(peer_id) is not int or peer_id == 0 or type(photo_id) is not int or photo_id <= 0:
                directory.rmdir()
                raise ValueError('numeric_avatar_source_required')
            meta.update(peer_id=peer_id, photo_id=photo_id)
        try:
            fd = os.open(directory / 'blob', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            write_json(directory / 'metadata.json', meta)
        except BaseException:
            shutil.rmtree(directory)
            raise
        return self.public_metadata(meta)

    def load(self, agent, staging_id, *, kind=None):
        directory = self.entry(staging_id)
        secure_directory(directory, create=False)
        meta = json.loads(read_private(directory / 'metadata.json', 8192))
        if (meta.get('agent') != agent or meta.get('staging_id') != staging_id
                or meta.get('expires_at', 0) <= time.time()
                or (kind is not None and meta.get('kind') != kind)):
            raise PermissionError('staging_wrong_agent_kind_or_expired')
        validate_file_metadata(meta.get('file_name'), meta.get('mime_type'), meta.get('size'),
                               meta.get('sha256'), self.maximum)
        data = read_private(directory / 'blob', self.maximum)
        if len(data) != meta['size'] or hashlib.sha256(data).hexdigest() != meta['sha256']:
            raise ValueError('staged_file_integrity_failed')
        return meta, data

    @staticmethod
    def public_metadata(meta):
        return {k: v for k, v in meta.items() if k != 'agent'}


def allowed_url(url, allowed_hosts):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if (not isinstance(url, str) or len(url) > 16384 or parsed.scheme != 'https'
                or not host or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.fragment
                or any(ord(c) < 32 or ord(c) == 127 for c in url)):
            raise ValueError()
        if host.lower() not in allowed_hosts:
            raise ValueError('file_download_host_not_allowed')
        # HTTPS host allowlist plus DNS/IP validation, with connection pinned below.
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError('file_download_address_not_public')
        return parsed, addresses
    except ValueError as exc:
        if str(exc) in ('file_download_host_not_allowed', 'file_download_address_not_public'):
            raise
        raise ValueError('invalid_file_download_url') from None
    except Exception:
        raise ValueError('file_download_dns_failed') from None


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, timeout=15, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        family, socktype, proto, _, sockaddr = self.address
        raw = socket.socket(family, socktype, proto)
        try:
            raw.settimeout(self.timeout)
            raw.connect(sockaddr)
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def fetch_host_file(file, expected_size, expected_sha256, allowed_hosts):
    """No proxy, redirects, cookies or URL logs. Pinned public IP and normal TLS."""
    validate_file_metadata(file.file_name or 'attachment.bin', file.mime_type or 'application/octet-stream',
                           expected_size, expected_sha256)
    if not file.file_id or len(file.file_id) > 512:
        raise ValueError('host_file_reference_required')
    parsed, addresses = allowed_url(file.download_url, allowed_hosts)
    connection = PinnedHTTPS(parsed.hostname, addresses[0])
    try:
        connection.request('GET', (parsed.path or '/') + ('?' + parsed.query if parsed.query else ''),
                           headers={'Accept-Encoding': 'identity'})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('file_host_http_failure_or_redirect')
        if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
            raise ValueError('encoded_file_response_not_allowed')
        length = response.getheader('Content-Length')
        if length is not None and (not length.isdecimal() or int(length) != expected_size):
            raise ValueError('host_file_size_mismatch')
        chunks, size, deadline = [], 0, time.monotonic() + 40
        while True:
            if time.monotonic() >= deadline:
                raise ValueError('host_file_deadline_exceeded')
            piece = response.read(min(65536, expected_size + 1 - size))
            if not piece:
                break
            chunks.append(piece); size += len(piece)
            if size > expected_size:
                raise ValueError('host_file_size_mismatch')
        data = b''.join(chunks)
        if size != expected_size or hashlib.sha256(data).hexdigest() != expected_sha256:
            raise ValueError('host_file_integrity_failed')
        return data
    except ValueError as exc:
        if str(exc) in {'file_host_http_failure_or_redirect', 'encoded_file_response_not_allowed',
                        'host_file_size_mismatch', 'host_file_deadline_exceeded', 'host_file_integrity_failed'}:
            raise
        raise ValueError('file_host_transport_failed') from None
    except Exception:
        # Exception strings can include temporary signed URLs: never return them.
        raise ValueError('file_host_transport_failed') from None
    finally:
        connection.close()
