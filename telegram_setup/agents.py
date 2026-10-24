"""Owner-managed, revocable reader identities; never print a bearer token."""
import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
import time
from pathlib import Path

ACTIONS = frozenset({'status', 'dialogs', 'contacts', 'history', 'search', 'messages'})


class AccessDenied(PermissionError):
    pass


class AgentRegistry:
    def __init__(self, path):
        self.path = Path(path).expanduser().absolute()

    def load(self):
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > 262144):
                raise AccessDenied('agent_registry_unsafe')
            data = json.load(stream)
        if (not isinstance(data, dict) or type(data.get('schema_version')) is not int or data.get('schema_version') != 1
                or not isinstance(data.get('agents'), dict)):
            raise AccessDenied('agent_registry_invalid')
        for name, row in data['agents'].items():
            if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', name) or not isinstance(row, dict):
                raise AccessDenied('agent_registry_invalid')
            if type(row.get('enabled')) is not bool:
                raise AccessDenied('agent_registry_invalid')
            digest = row.get('token_sha256')
            if digest is not None and (not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest)):
                raise AccessDenied('agent_registry_invalid')
            if row['enabled'] and digest is None:
                raise AccessDenied('agent_registry_invalid')
            for key in ('all_chats', 'all_contacts', 'include_phones'):
                if type(row.get(key, False)) is not bool:
                    raise AccessDenied('agent_registry_invalid')
            for key in ('chat_ids', 'contact_ids'):
                ids = row.get(key, [])
                if (not isinstance(ids, list) or len(ids) > 10000
                        or any(type(x) is not int or x == 0 for x in ids)
                        or (key == 'contact_ids' and any(x < 0 for x in ids))):
                    raise AccessDenied('agent_registry_invalid')
            actions = row.get('actions', [])
            if not isinstance(actions, list) or not all(isinstance(x, str) for x in actions) or not set(actions) <= ACTIONS:
                raise AccessDenied('agent_registry_invalid')
            expiry = row.get('expires_at')
            if expiry is not None and (type(expiry) is not int or expiry <= 0):
                raise AccessDenied('agent_registry_invalid')
        return data

    def profile(self, agent):
        row = self.load()['agents'].get(agent)
        if (not row or not row['enabled'] or
                (row.get('expires_at') is not None and row['expires_at'] <= time.time())):
            raise AccessDenied('agent_disabled_or_expired')
        return dict(row, agent=agent)

    def authenticate(self, token):
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            raise AccessDenied('authentication_required')
        digest = hashlib.sha256(token.encode()).hexdigest()
        match = None
        for name, row in self.load()['agents'].items():
            if row.get('token_sha256') and hmac.compare_digest(digest, row['token_sha256']):
                if match is not None:
                    raise AccessDenied('ambiguous_agent_token')
                match = name
        if match is None:
            raise AccessDenied('authentication_required')
        self.profile(match)
        return match

    def save(self, data):
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if parent.is_symlink() or parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o077:
            raise AccessDenied('agent_registry_directory_unsafe')
        fd, temporary = tempfile.mkstemp(prefix='.agents-', dir=parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(data, stream, indent=2)
                stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def check_action(profile, action, chat_id=None):
    if action not in ACTIONS or action not in profile.get('actions', []):
        raise AccessDenied('action_not_permitted')
    if chat_id is not None:
        if type(chat_id) is not int or chat_id == 0:
            raise AccessDenied('numeric_chat_id_required')
        if not profile.get('all_chats', False) and chat_id not in profile.get('chat_ids', []):
            raise AccessDenied('chat_not_permitted')


def main():
    from .config import prepare_private
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry')
    sub = parser.add_subparsers(dest='action', required=True)
    add = sub.add_parser('add')
    add.add_argument('agent'); add.add_argument('--chat-id', action='append', type=int, default=[])
    add.add_argument('--contact-id', action='append', type=int, default=[])
    add.add_argument('--all-chats', action='store_true'); add.add_argument('--all-contacts', action='store_true')
    add.add_argument('--include-phones', action='store_true')
    add.add_argument('--expires-days', type=int, default=30)
    add.add_argument('--token-file', required=True)
    revoke = sub.add_parser('revoke'); revoke.add_argument('agent')
    sub.add_parser('show')
    args = parser.parse_args()
    registry = AgentRegistry(args.registry or prepare_private() / 'agents.json')
    data = registry.load() if registry.path.exists() else {'schema_version': 1, 'agents': {}}
    if args.action == 'show':
        print(json.dumps({name: {'enabled': row['enabled'], 'expires_at': row.get('expires_at'),
            'all_chats': row.get('all_chats', False), 'chat_count': len(row.get('chat_ids', [])),
            'all_contacts': row.get('all_contacts', False), 'contact_count': len(row.get('contact_ids', []))}
            for name, row in data['agents'].items()}))
        return
    if args.action == 'revoke':
        if args.agent not in data['agents']:
            parser.error('Unknown agent')
        data['agents'][args.agent]['enabled'] = False
        registry.save(data)
        print('Agent revoked; subsequent requests are denied.')
        return
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', args.agent) or not 1 <= args.expires_days <= 365:
        parser.error('Invalid agent or lifetime')
    if args.agent in data['agents']:
        parser.error('Agent already exists; use a new identity or revoke it first.')
    if any(x == 0 for x in args.chat_id) or any(x <= 0 for x in args.contact_id):
        parser.error('Invalid chat/contact ID')
    token_path = Path(args.token_file).expanduser().absolute()
    token = secrets.token_urlsafe(32)
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(token + '\n')
        data['agents'][args.agent] = {'enabled': True,
            'token_sha256': hashlib.sha256(token.encode()).hexdigest(),
            'expires_at': int(time.time()) + args.expires_days * 86400,
            'all_chats': args.all_chats, 'chat_ids': sorted(set(args.chat_id)),
            'all_contacts': args.all_contacts, 'contact_ids': sorted(set(args.contact_id)),
            'include_phones': args.include_phones, 'actions': sorted(ACTIONS)}
        registry.save(data)
    except BaseException:
        token_path.unlink(missing_ok=True)
        raise
    print(json.dumps({'agent': args.agent, 'token_file': str(token_path),
                      'raw_token_printed': False, 'read_only': True}))


if __name__ == '__main__':
    main()
