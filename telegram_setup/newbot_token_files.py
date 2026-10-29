"""POSIX file storage for a broker, never a model-facing token retrieval tool.

Files are not encrypted and processes with the same UID can read them. The
caller must verify bot ownership and owner consent before storing a real token.
Only the broker may call load_for_broker; this module registers no MCP tools.
"""
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat

try:
    import fcntl
except ImportError:  # The package supports macOS/Linux, not a Windows vault.
    fcntl = None

from .poll_common import valid_request_id


_TOKEN = re.compile(r"([1-9][0-9]{4,18}):[A-Za-z0-9_-]{30,128}\Z")
_REF = re.compile(r"botcred_([0-9a-f]{64})\Z")
_MAX_FILE_BYTES = 4096


class TokenStoreError(RuntimeError):
    """A fixed error code; secret values and filesystem paths are never included."""


def _unique_members(pairs):
    if len(pairs) != len({key for key, _ in pairs}):
        raise TokenStoreError("corrupt_token_file")
    return dict(pairs)


def _token_bot_id(secret):
    match = _TOKEN.fullmatch(secret) if isinstance(secret, str) else None
    if match is None or int(match[1]) >= 2**63:
        raise TokenStoreError("invalid_bot_token")
    return int(match[1])


def _reference(intent_id):
    try:
        valid_request_id(intent_id)
    except (ValueError, TypeError):
        raise TokenStoreError("invalid_intent_id") from None
    # This is an identifier, not a bearer credential or an authentication proof.
    digest = hashlib.sha256(("telegram-newbot\0" + intent_id).encode()).hexdigest()
    return "botcred_" + digest


class FileBotTokenStore:
    """Store one token per intent, without replacing an existing credential.

    The operator chooses an existing parent directory. A private leaf directory
    is created lazily; unsafe existing modes are rejected, not silently changed.
    fsync failure is an unknown storage outcome and never returns a reference.
    """

    def __init__(self, directory):
        self._directory = Path(directory).absolute()

    def for_intent(self, intent_id):
        return _BoundIntentStore(self, intent_id)

    @staticmethod
    def reference_for_intent(intent_id):
        """Reserve an opaque reference before any Telegram operation."""
        return _reference(intent_id)

    @contextmanager
    def _root(self, *, create):
        if fcntl is None or not all(hasattr(os, n) for n in ("O_NOFOLLOW", "O_DIRECTORY")):
            raise TokenStoreError("posix_file_store_required")
        if create:
            try:
                self._directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(self._directory, flags)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise TokenStoreError("unsafe_token_directory")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise TokenStoreError("token_store_busy") from None
            yield fd
        finally:
            os.close(fd)  # Closing releases this descriptor's flock.

    def _read(self, directory_fd, reference):
        match = _REF.fullmatch(reference) if isinstance(reference, str) else None
        if match is None:
            raise TokenStoreError("invalid_token_reference")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(reference + ".json", flags, dir_fd=directory_fd)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise TokenStoreError("unsafe_token_file")
            if not 0 < info.st_size <= _MAX_FILE_BYTES:
                raise TokenStoreError("corrupt_token_file")
            raw = bytearray()
            while len(raw) <= _MAX_FILE_BYTES:
                chunk = os.read(fd, min(1024, _MAX_FILE_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > _MAX_FILE_BYTES:
                raise TokenStoreError("corrupt_token_file")
            try:
                value = json.loads(raw, object_pairs_hook=_unique_members)
            except (ValueError, UnicodeError):
                raise TokenStoreError("corrupt_token_file") from None
            if (not isinstance(value, dict)
                    or set(value) != {"version", "intent_digest", "bot_id", "token"}
                    or type(value["version"]) is not int or value["version"] != 1
                    or value["intent_digest"] != match[1]
                    or type(value["bot_id"]) is not int
                    or value["bot_id"] != _token_bot_id(value["token"])):
                raise TokenStoreError("corrupt_token_file")
            return value, fd
        except BaseException:
            os.close(fd)
            raise

    def store(self, secret, *, intent_id):
        reference = _reference(intent_id)
        bot_id = _token_bot_id(secret)
        try:
            with self._root(create=True) as directory_fd:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
                try:
                    fd = os.open(reference + ".json", flags, 0o600, dir_fd=directory_fd)
                except FileExistsError:
                    existing, fd = self._read(directory_fd, reference)
                    try:
                        if not hmac.compare_digest(existing["token"], secret):
                            raise TokenStoreError("intent_token_conflict")
                        os.fsync(fd)
                        os.fsync(directory_fd)
                        return reference
                    finally:
                        os.close(fd)
                try:
                    value = {"version": 1, "intent_digest": reference[8:],
                             "bot_id": bot_id, "token": secret}
                    raw = (json.dumps(value, separators=(",", ":")) + "\n").encode()
                    # Exact owner-only mode even under an unusual umask.
                    os.fchmod(fd, 0o600)
                    while raw:
                        written = os.write(fd, raw)
                        if written <= 0:
                            raise TokenStoreError("token_store_write_failed")
                        raw = raw[written:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.fsync(directory_fd)
                return reference
        except TokenStoreError:
            raise
        except (OSError, ValueError, UnicodeError, TypeError):
            # A partial exclusive claim remains closed; never overwrite it.
            raise TokenStoreError("token_store_io_failed") from None

    def load_for_broker(self, reference):
        """Internal use only. Never register this method as an MCP tool."""
        try:
            with self._root(create=False) as directory_fd:
                value, fd = self._read(directory_fd, reference)
                try:
                    return value["token"]
                finally:
                    os.close(fd)
        except TokenStoreError:
            raise
        except (OSError, ValueError, UnicodeError, TypeError):
            raise TokenStoreError("token_reference_unavailable") from None

    def metadata(self, reference):
        """No ownership claim: only the numeric token prefix is known here."""
        secret = self.load_for_broker(reference)
        return {"token_ref": reference, "token_bot_id": _token_bot_id(secret),
                "storage": "posix_files", "file_mode": "0600",
                "directory_mode": "0700", "encrypted": False}


class _BoundIntentStore:
    def __init__(self, store, intent_id):
        _reference(intent_id)
        self._store = store
        self._intent_id = intent_id

    def store(self, secret):
        return self._store.store(secret, intent_id=self._intent_id)
