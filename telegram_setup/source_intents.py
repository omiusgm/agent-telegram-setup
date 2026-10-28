"""Local, short-lived owner-origin intents for adding Telegram sources.

This is a routing check, not OS isolation: the owner and agents share a Unix UID.
The file contains invite links and must never be published or logged.
"""
import os
from contextlib import closing
import re
import sqlite3
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit

_USER = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}\Z")
_INVITE = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")
_URL = re.compile(r"(?i)(?:https?://)?(?:www\.)?t\.me/[^\s<>\"']+|(?<![\w@])@[A-Za-z][A-Za-z0-9_]{3,31}")
_RESERVED = {'c', 's', 'joinchat', 'share', 'addstickers', 'addemoji', 'proxy', 'iv'}


def normalize_source(value):
    if not isinstance(value, str) or len(value) > 256:
        raise ValueError('invalid_source')
    value = value.strip().rstrip('.,;:!?)]}')
    if value.startswith('@'):
        name = value[1:]
        if not _USER.fullmatch(name) or name.lower() in _RESERVED:
            raise ValueError('invalid_source')
        return 'https://t.me/' + name.lower()
    if not re.match(r'(?i)^https?://', value):
        value = 'https://' + value
    u = urlsplit(value)
    if u.scheme.lower() not in ('http', 'https') or u.hostname not in ('t.me', 'www.t.me') or u.username or u.password or u.port:
        raise ValueError('invalid_source')
    parts = [p for p in u.path.split('/') if p]
    if not parts:
        raise ValueError('invalid_source')
    if parts[0].lower() == 'joinchat' and len(parts) == 2:
        token = parts[1]
    elif parts[0].startswith('+') and len(parts) == 1:
        token = parts[0][1:]
    else:
        token = None
    if token is not None:
        if not _INVITE.fullmatch(token):
            raise ValueError('invalid_source')
        return 'https://t.me/+' + token
    if len(parts) > 2 or len(parts) == 2 and not parts[1].isdigit():
        raise ValueError('invalid_source')
    name = parts[0]
    if not _USER.fullmatch(name) or name.lower() in _RESERVED:
        raise ValueError('invalid_source')
    return 'https://t.me/' + name.lower()


def extract_sources(text):
    result = []
    for match in _URL.finditer(text or ''):
        try:
            url = normalize_source(match.group())
            if url not in result:
                result.append(url)
        except ValueError:
            pass
        if len(result) >= 10:
            break
    return result


def _database(home):
    home = Path(home)
    info = home.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('private_directory_unsafe')
    p = home / 'source-intents.sqlite3'
    try:
        fd = os.open(p, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    except FileExistsError:
        info = p.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError('intent_file_unsafe')
    db = sqlite3.connect(p, timeout=5)
    db.execute('CREATE TABLE IF NOT EXISTS intents (source TEXT NOT NULL, message_id TEXT NOT NULL, created REAL NOT NULL, status TEXT NOT NULL, PRIMARY KEY(source,message_id))')
    return db


def record(home, source, message_id, now=None):
    source = normalize_source(source)
    if not str(message_id).isdigit():
        raise ValueError('invalid_message_id')
    with closing(_database(home)) as db:
        with db:
            db.execute('INSERT OR IGNORE INTO intents VALUES (?,?,?,?)', (source, str(message_id), time.time() if now is None else now, 'pending'))


def claim(home, source, now=None, ttl=43200):
    source = normalize_source(source)
    now = time.time() if now is None else now
    db = _database(home)
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT message_id FROM intents WHERE source=? AND status=? AND created>=? AND created<=? ORDER BY created DESC LIMIT 1', (source, 'pending', now-ttl, now)).fetchone()
        if not row:
            raise ValueError('owner_request_required')
        db.execute('UPDATE intents SET status=? WHERE source=? AND message_id=? AND status=?', ('in_progress', source, row[0], 'pending'))
        db.commit()
        return row[0]
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def finish(home, source, message_id, status):
    if status not in ('joined', 'already_joined', 'approval_pending', 'failed', 'uncertain'):
        raise ValueError('invalid_status')
    with closing(_database(home)) as db:
        with db:
            db.execute('UPDATE intents SET status=? WHERE source=? AND message_id=? AND status=?', (status, normalize_source(source), str(message_id), 'in_progress'))


def defer(home, source, message_id, seconds):
    with closing(_database(home)) as db:
        with db:
            db.execute('UPDATE intents SET status=?, created=? WHERE source=? AND message_id=? AND status=?', ('pending', time.time()+max(1, int(seconds)), normalize_source(source), str(message_id), 'in_progress'))
