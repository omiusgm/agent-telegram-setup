"""Server-local, mode-restricted bot credentials and one-shot operation state.

No tool exposes token bytes. Same-UID SSH/processes can still read these files;
this is not an OS isolation or authenticated-human approval boundary.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from .newbot_protocol import newbot_plan
from .newbot_token_files import FileBotTokenStore
from .private_state import read_private, secure_directory, write_json

_AGENT = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')


def _sync(folder):
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)


def _binding(agent, name, username, request_id):
    if not isinstance(agent, str) or not _AGENT.fullmatch(agent): raise ValueError('invalid_agent')
    plan = newbot_plan(name, username, request_id)
    raw = json.dumps({'agent': agent, **plan}, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(raw).hexdigest()


def _key(agent, request_id): return hashlib.sha256((agent + '\0' + request_id).encode()).hexdigest()


def _create_json(folder, name, value):
    folder = secure_directory(folder)
    parent_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
            stream.flush(); os.fsync(stream.fileno())
        os.fsync(parent_fd)
    finally: os.close(parent_fd)


class OperatorApprovals:
    """Operator assertion only; it is not proof of a human behind a model call."""
    def __init__(self, folder): self.folder = Path(folder)

    def issue(self, agent, name, username, request_id, *, ttl=300):
        if type(ttl) is not int or not 1 <= ttl <= 900: raise ValueError('invalid_approval_ttl')
        binding = _binding(agent, name, username, request_id)
        key = _key(agent, request_id)
        folder = secure_directory(self.folder)
        if (folder / (key + '.used')).exists(): raise ValueError('approval_already_consumed')
        _create_json(folder, key + '.json', {'binding': binding, 'expires_at': int(time.time()) + ttl,
                     'source': 'operator_assertion_not_human_authentication'})
        return {'request_id': request_id, 'expires_in_seconds': ttl}

    def check(self, agent, name, username, request_id):
        binding = _binding(agent, name, username, request_id)
        key = _key(agent, request_id)
        folder = secure_directory(self.folder, create=False)
        source, used = folder / (key + '.json'), folder / (key + '.used')
        if used.exists(): raise PermissionError('operator_approval_already_used')
        record = json.loads(read_private(source, 4096))
        if (record.get('binding') != binding or type(record.get('expires_at')) is not int
                or record['expires_at'] <= int(time.time())):
            raise PermissionError('exact_operator_approval_required')
        return {'request_id': request_id, 'expires_at': record['expires_at'],
                'source': 'operator_assertion_not_human_authentication'}

    def consume(self, agent, name, username, request_id):
        self.check(agent, name, username, request_id)
        key = _key(agent, request_id)
        folder = secure_directory(self.folder, create=False)
        source, used = folder / (key + '.json'), folder / (key + '.used')
        # An exclusive hard link is the one-shot marker. Crash after link but
        # before unlink still fails closed because the used marker exists.
        os.link(source, used, follow_symlinks=False)
        _sync(folder)
        linked = json.loads(read_private(used, 4096))
        if (linked.get('binding') != _binding(agent, name, username, request_id)
                or type(linked.get('expires_at')) is not int or linked['expires_at'] <= int(time.time())):
            raise PermissionError('exact_operator_approval_required')
        source.unlink(); _sync(folder)


class NewBotClaims:
    """One unresolved creation per account; durable claim precedes any send."""
    def __init__(self, folder): self.folder = Path(folder); self.fd = None

    def __enter__(self):
        root = secure_directory(self.folder)
        fd = os.open(root / '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            os.close(fd); raise PermissionError('newbot_lock_unsafe')
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException: os.close(fd); raise
        self.fd = fd; return self

    def __exit__(self, *_):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN); os.close(self.fd); self.fd = None

    def _read(self, path): return json.loads(read_private(path, 8192))

    def find(self, agent, name, username, request_id):
        if self.fd is None: raise RuntimeError('newbot_claim_lock_required')
        binding, key = _binding(agent, name, username, request_id), _key(agent, request_id)
        root = secure_directory(self.folder, create=False)
        try:
            prior = self._read(root / (key + '.json'))
        except FileNotFoundError:
            prior = None
        if prior is not None:
            if prior.get('binding') != binding: raise ValueError('request_id_reused_with_different_plan')
            return prior
        try:
            pending = self._read(root / 'pending.json')
        except FileNotFoundError:
            pending = None
        if pending is not None:
            if pending.get('binding') == binding and pending.get('request_id') == request_id:
                return pending
            raise PermissionError('another_newbot_attempt_unresolved')
        username_key = hashlib.sha256(username.casefold().encode()).hexdigest()
        if (root / ('username-' + username_key + '.json')).exists():
            raise PermissionError('username_already_attempted')
        return None

    def begin(self, agent, name, username, request_id, approvals, *, authorization_mode='operator'):
        if self.fd is None: raise RuntimeError('newbot_claim_lock_required')
        if authorization_mode not in ('operator', 'delegated'):
            raise ValueError('invalid_newbot_authorization_mode')
        binding, key = _binding(agent, name, username, request_id), _key(agent, request_id)
        root = secure_directory(self.folder, create=False)
        receipt_path, pending = root / (key + '.json'), root / 'pending.json'
        try:
            prior = self._read(receipt_path)
        except FileNotFoundError:
            prior = None
        if prior is not None:
            if prior.get('binding') != binding: raise ValueError('request_id_reused_with_different_plan')
            return prior, False
        try:
            prior_pending = self._read(pending)
        except FileNotFoundError:
            prior_pending = None
        if prior_pending is not None:
            if prior_pending.get('binding') == binding and prior_pending.get('request_id') == request_id:
                return prior_pending, False
            raise PermissionError('another_newbot_attempt_unresolved')
        username_key = hashlib.sha256(username.casefold().encode()).hexdigest()
        if (root / ('username-' + username_key + '.json')).exists():
            raise PermissionError('username_already_attempted')
        approvals.consume(agent, name, username, request_id)
        token_intent = 'newbot-' + _key(agent, request_id)
        claim = {'binding': binding, 'agent': agent, 'request_id': request_id,
                 'username_hash': username_key, 'token_intent': token_intent,
                 'reserved_ref': FileBotTokenStore.reference_for_intent(token_intent),
                 'authorization_mode': authorization_mode, 'owner_identity_verified': False,
                 'status': 'uncertain_do_not_retry', 'readback_verified': False}
        _create_json(root, 'pending.json', claim)
        _create_json(root, 'username-' + username_key + '.json', {'binding': binding})
        _create_json(root, key + '.json', claim)
        return claim, True

    def finish_verified(self, claim, bot_id, username):
        if self.fd is None or type(bot_id) is not int or bot_id <= 0: raise ValueError('invalid_newbot_receipt')
        root = secure_directory(self.folder, create=False)
        current = self._read(root / 'pending.json')
        if (current.get('binding') != claim.get('binding') or current.get('reserved_ref') != claim.get('reserved_ref')
                or not isinstance(username, str)
                or hashlib.sha256(username.casefold().encode()).hexdigest() != claim.get('username_hash')):
            raise RuntimeError('newbot_claim_changed')
        receipt = {**claim, 'status': 'created_verified', 'readback_verified': True,
                   'bot_id': bot_id, 'username': username, 'token_ref': claim['reserved_ref']}
        write_json(root / (_key(claim['agent'], claim['request_id']) + '.json'), receipt)
        (root / 'pending.json').unlink(); _sync(root)
        return receipt


def public_receipt(record):
    allowed = ('request_id', 'status', 'readback_verified', 'bot_id', 'username', 'token_ref',
               'authorization_mode', 'owner_identity_verified')
    result = {k: record[k] for k in allowed if k in record}
    if result.get('status') != 'created_verified': result.pop('token_ref', None)
    return result
