"""Save one owner-entered tunnel runtime key to a new private file.

Accepts hidden Terminal input or a local stdin pipe, never an argument or env
variable. The result is a file reference only; no key is printed. This does
not validate permissions, contact OpenAI, or start a tunnel.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import stat
import sys
import warnings

MAX_BYTES = 4096
FILENAME = 'runtime.token'


class SafeParser(argparse.ArgumentParser):
    def error(self, _message):
        # Even an accidentally pasted positional value must not be echoed.
        self.exit(2, 'Invalid arguments. Use --help for the safe input method.\n')


def read_key():
    if sys.stdin.isatty():
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            value = getpass.getpass('Вставьте runtime key (ввод скрыт): ').encode('utf-8')
    else:
        value = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(value) > MAX_BYTES:
        raise ValueError('invalid_input')
    value = value.rstrip(b'\r\n')
    # One visible-ASCII value, not two pasted values, newlines or a JSON blob.
    if (not 16 <= len(value) <= 1024 or any(c < 33 or c > 126 for c in value)
            or any(c in value for c in (b'{', b'}', b'[', b']', b'"', b'\\'))):
        raise ValueError('invalid_input')
    return value


def save_new(directory, value):
    path = Path(directory).expanduser()
    if not path.is_absolute() or path.name in ('', '.', '..'):
        raise ValueError('absolute_private_directory_required')
    parent = path.parent.resolve(strict=True)
    # Never save under a checked-out repository, including worktree .git files.
    if any((part / '.git').exists() or (part / '.git').is_symlink() for part in (parent, *parent.parents)):
        raise PermissionError('directory_inside_git_checkout')
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    dir_fd = None
    try:
        info = os.fstat(parent_fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (os.geteuid(), 0)
                or info.st_mode & 0o022):
            raise PermissionError('unsafe_parent_directory')
        created_directory = False
        try:
            os.mkdir(path.name, 0o700, dir_fd=parent_fd)
            created_directory = True
        except FileExistsError:
            pass
        if created_directory:
            os.fsync(parent_fd)
        dir_fd = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        info = os.fstat(dir_fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise PermissionError('unsafe_private_directory')
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(FILENAME, flags, 0o600, dir_fd=dir_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PermissionError('unsafe_new_key_file')
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                fd = None
                stream.write(value); stream.flush(); os.fsync(stream.fileno())
            os.fsync(dir_fd)
        except BaseException:
            if fd is not None: os.close(fd)
            # Remove only our own newly created inode, never a replacement.
            try:
                current = os.stat(FILENAME, dir_fd=dir_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
                    os.unlink(FILENAME, dir_fd=dir_fd)
            except OSError:
                pass
            raise
    finally:
        if dir_fd is not None: os.close(dir_fd)
        os.close(parent_fd)


def main():
    parser = SafeParser(description=__doc__)
    parser.add_argument('--directory', required=True, help='Absolute private directory outside a Git checkout')
    args = parser.parse_args()
    try:
        key = read_key()
        save_new(args.directory, key)
        key = None
    except FileExistsError:
        print(json.dumps({'status': 'not_saved', 'reason': 'already_exists'}))
        return 1
    except (OSError, ValueError, PermissionError, EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        print(json.dumps({'status': 'not_saved', 'reason': 'input_or_private_storage_error'}))
        return 1
    print(json.dumps({'status': 'saved', 'secret_printed': False}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
