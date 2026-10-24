"""Stdio clients share one local account process through a private Unix socket."""

import errno
import fcntl
import hashlib
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import ROOT, prepare_private, private_dir


def socket_path():
    # macOS limits Unix socket paths to 104 bytes. Deep project paths do not fit.
    key = hashlib.sha256(str(private_dir()).encode()).hexdigest()[:20]
    folder = Path('/tmp') / f'agent-telegram-{os.getuid()}-{key}'
    folder.mkdir(mode=0o700, exist_ok=True)
    info = folder.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise RuntimeError('Private socket directory has unsafe ownership or permissions')
    return folder / 'mcp.sock'


def connect(path):
    connection = socket.socket(socket.AF_UNIX)
    try:
        connection.settimeout(2)
        connection.connect(str(path))
        connection.settimeout(None)
        return connection
    except OSError as exc:
        connection.close()
        if exc.errno in {errno.ENOENT, errno.ECONNREFUSED}:
            return None
        raise


def open_connection():
    folder = prepare_private()
    path = socket_path()
    connection = connect(path)
    if connection is not None:
        return connection
    if os.environ.get('AGENT_TELEGRAM_MANAGED_BACKEND') == '1':
        raise RuntimeError('Managed Telegram backend is not ready; wait for its service')
    deadline = time.monotonic() + 60
    with (folder / 'shared-start.lock').open('a') as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Shared Telegram startup is still busy') from None
                time.sleep(0.1)
        connection = connect(path)
        if connection is not None:
            return connection
        log_path = folder / 'shared-server.log'
        with log_path.open('ab') as log:
            log_path.chmod(0o600)
            child = subprocess.Popen([sys.executable, '-m', 'telegram_setup.shared'],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=log, start_new_session=True)
        while time.monotonic() < deadline:
            if child.poll() is not None:
                break
            connection = connect(path)
            if connection is not None:
                return connection
            time.sleep(0.2)
    raise RuntimeError('Shared Telegram unavailable; inspect the private shared-server.log locally')


def main():
    try:
        connection = open_connection()

        def upload():
            try:
                for line in sys.stdin.buffer:
                    connection.sendall(line)
            except OSError:
                pass
            finally:
                try:
                    connection.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        threading.Thread(target=upload, daemon=True).start()
        try:
            with connection.makefile('rb') as reader:
                for line in reader:
                    sys.stdout.buffer.write(line)
                    sys.stdout.buffer.flush()
        finally:
            connection.close()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print('MCP relay failed: ' + type(exc).__name__ +
              '. Inspect this installation locally; do not retry uncertain writes.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
