"""Small private, atomic JSON records. Never store credentials or source URLs."""
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile


def secure_directory(folder, *, create=True):
    folder = Path(folder).absolute()
    if create:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError('unsafe_private_directory')
    return folder


def read_private(path, maximum=2097152):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > maximum):
            raise PermissionError('unsafe_private_file')
        return stream.read(maximum + 1)


def write_json(path, value):
    folder = secure_directory(Path(path).parent)
    fd, temporary = tempfile.mkstemp(prefix='.state-', dir=folder)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def opaque_id():
    return secrets.token_urlsafe(24)
