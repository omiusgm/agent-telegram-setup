"""Revocable agent identities; reads by default, explicit Saved Messages text."""
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

READ_ACTIONS = frozenset({'status', 'dialogs', 'contacts', 'history', 'search', 'messages'})
SAVED_TEXT_ACTION = 'send-saved-text'
OPTIONAL_ACTIONS = frozenset({'public-search', 'send-saved-file', 'download-saved-file', 'read-photo',
                              'read-profiles', 'read-avatars', 'read-attachments',
                              'read-polls', 'create-polls', 'close-own-polls'})
ACTIONS = READ_ACTIONS | {SAVED_TEXT_ACTION} | OPTIONAL_ACTIONS


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
            for key in ('all_chats', 'all_contacts', 'include_phones', 'poll_write_saved', 'poll_write_all_chats'):
                if type(row.get(key, False)) is not bool:
                    raise AccessDenied('agent_registry_invalid')
            for key in ('chat_ids', 'contact_ids'):
                ids = row.get(key, [])
                if (not isinstance(ids, list) or len(ids) > 10000
                        or any(type(x) is not int or x == 0 for x in ids)
                        or (key == 'contact_ids' and any(x < 0 for x in ids))):
                    raise AccessDenied('agent_registry_invalid')
            poll_ids = row.get('poll_write_chat_ids', [])
            if (not isinstance(poll_ids, list) or len(poll_ids) > 1000
                    or any(type(x) is not int or x >= 0 for x in poll_ids)):
                raise AccessDenied('agent_registry_invalid')
            actions = row.get('actions', [])
            sources = row.get('photo_sources', [])
            if (not isinstance(sources, list) or len(sources) > 200 or any(
                    not isinstance(s, dict) or set(s) != {'chat_id', 'message_id'}
                    or type(s['chat_id']) is not int or s['chat_id'] == 0
                    or type(s['message_id']) is not int or s['message_id'] <= 0 for s in sources)):
                raise AccessDenied('agent_photo_sources_invalid')
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


def check_photo(profile, chat_id, message_id):
    check_action(profile, 'read-photo', chat_id)
    if type(message_id) is not int or message_id <= 0:
        raise AccessDenied('positive_message_id_required')
    if {'chat_id': chat_id, 'message_id': message_id} not in profile.get('photo_sources', []):
        raise AccessDenied('photo_source_not_permitted')


def check_poll_target(profile, action, chat_id, owner_id, *, write):
    """Poll writes need their own exact targets, independently of all_chats."""
    check_action(profile, action)
    if type(chat_id) is not int or chat_id == 0 or type(owner_id) is not int or owner_id <= 0:
        raise AccessDenied('numeric_chat_id_required')
    if chat_id == owner_id and (profile.get('poll_write_saved', False) or profile.get('poll_write_all_chats', False)):
        check_action(profile, 'history')
        return
    check_action(profile, 'history', chat_id)
    if write and not (profile.get('poll_write_all_chats', False)
                      or (chat_id < 0 and chat_id in profile.get('poll_write_chat_ids', []))):
        raise AccessDenied('poll_destination_not_permitted')


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
    add.add_argument('--poll-saved', action='store_true', help='Permit separately granted poll writes to Saved Messages')
    add.add_argument('--poll-all-chats', action='store_true', help='Explicitly permit separately granted poll writes in all readable chats')
    add.add_argument('--poll-chat-id', action='append', type=int, default=[], help='Exact group/channel target for separately granted poll writes')
    add.add_argument('--expires-days', type=int, default=30)
    add.add_argument('--token-file', required=True)
    add.add_argument('--send-saved-text', action='store_true',
                     help='Explicitly allow plain text only to this account\'s Saved Messages')
    for name in sorted(OPTIONAL_ACTIONS):
        if name == 'read-photo':
            continue  # Exact sources must be granted separately, never account-wide.
        add.add_argument('--' + name, action='store_true', help='Explicit optional agent capability')
    photo = sub.add_parser('grant-read-photo')
    photo.add_argument('agent'); photo.add_argument('--chat-id', type=int, required=True)
    photo.add_argument('--message-id', type=int, required=True)
    sub.add_parser('revoke-read-photo').add_argument('agent')
    revoke = sub.add_parser('revoke'); revoke.add_argument('agent')
    for name in ('grant-saved-text', 'revoke-saved-text'):
        sub.add_parser(name).add_argument('agent')
    for operation in ('grant', 'revoke'):
        sub.add_parser(operation + '-poll-saved').add_argument('agent')
        sub.add_parser(operation + '-poll-all-chats').add_argument('agent')
        target = sub.add_parser(operation + '-poll-chat')
        target.add_argument('agent'); target.add_argument('--chat-id', type=int, required=True)
    scopes = {'public-search': 'public-search', 'saved-upload': 'send-saved-file',
              'saved-download': 'download-saved-file', 'profiles': 'read-profiles', 'avatars': 'read-avatars',
              'attachments': 'read-attachments', 'poll-read': 'read-polls',
              'poll-create': 'create-polls', 'poll-close': 'close-own-polls'}
    for scope in scopes:
        for operation in ('grant', 'revoke'):
            sub.add_parser(operation + '-' + scope).add_argument('agent')
    sub.add_parser('show')
    args = parser.parse_args()
    registry = AgentRegistry(args.registry or prepare_private() / 'agents.json')
    data = registry.load() if registry.path.exists() else {'schema_version': 1, 'agents': {}}
    if args.action in ('grant-poll-saved', 'revoke-poll-saved', 'grant-poll-chat', 'revoke-poll-chat',
                       'grant-poll-all-chats', 'revoke-poll-all-chats'):
        if args.agent not in data['agents']:
            parser.error('Unknown agent')
        row = data['agents'][args.agent]
        if args.action.endswith('poll-saved'):
            row['poll_write_saved'] = args.action.startswith('grant-')
        elif args.action.endswith('poll-all-chats'):
            row['poll_write_all_chats'] = args.action.startswith('grant-')
        else:
            if args.chat_id >= 0:
                parser.error('Poll group/channel targets require a negative numeric chat ID')
            ids = set(row.get('poll_write_chat_ids', []))
            if args.action.startswith('grant-'): ids.add(args.chat_id)
            else: ids.discard(args.chat_id)
            if len(ids) > 1000: parser.error('At most 1000 exact poll targets')
            row['poll_write_chat_ids'] = sorted(ids)
        registry.save(data)
        print('Poll destination updated; no Telegram operation was performed.')
        return
    if args.action in ('grant-read-photo', 'revoke-read-photo'):
        if args.agent not in data['agents']:
            parser.error('Unknown agent')
        row = data['agents'][args.agent]
        actions = set(row.get('actions', []))
        if args.action == 'grant-read-photo':
            if args.chat_id == 0 or args.message_id <= 0:
                parser.error('Numeric chat and positive message IDs required')
            check_action(dict(row, actions=list(actions | {'read-photo'})), 'read-photo', args.chat_id)
            source = {'chat_id': args.chat_id, 'message_id': args.message_id}
            sources = list(row.get('photo_sources', []))
            if source not in sources:
                sources.append(source)
            if len(sources) > 200:
                parser.error('At most 200 exact photo sources')
            row['photo_sources'] = sources; actions.add('read-photo')
        else:
            row.pop('photo_sources', None); actions.discard('read-photo')
        row['actions'] = sorted(actions)
        registry.save(data)
        print('Exact photo permission updated; no Telegram read or send performed.')
        return
    if args.action == 'show':
        print(json.dumps({name: {'enabled': row['enabled'], 'expires_at': row.get('expires_at'),
            'all_chats': row.get('all_chats', False), 'chat_count': len(row.get('chat_ids', [])),
            'all_contacts': row.get('all_contacts', False), 'contact_count': len(row.get('contact_ids', [])),
            'send_saved_text': SAVED_TEXT_ACTION in row.get('actions', []),
            'poll_write_saved': row.get('poll_write_saved', False),
            'poll_write_all_chats': row.get('poll_write_all_chats', False),
            'poll_write_chat_count': len(row.get('poll_write_chat_ids', [])),
            'optional_actions': sorted(set(row.get('actions', [])) & OPTIONAL_ACTIONS)}
            for name, row in data['agents'].items()}))
        return
    if args.action == 'revoke':
        if args.agent not in data['agents']:
            parser.error('Unknown agent')
        data['agents'][args.agent]['enabled'] = False
        registry.save(data)
        print('Agent revoked; subsequent requests are denied.')
        return
    if args.action in ('grant-saved-text', 'revoke-saved-text'):
        if args.agent not in data['agents']:
            parser.error('Unknown agent')
        actions = set(data['agents'][args.agent]['actions'])
        if args.action == 'grant-saved-text':
            actions.add(SAVED_TEXT_ACTION)
        else:
            actions.discard(SAVED_TEXT_ACTION)
        data['agents'][args.agent]['actions'] = sorted(actions)
        registry.save(data)
        print('Saved Messages text permission updated; no Telegram message was sent.')
        return
    for scope, permission in scopes.items():
        if args.action in ('grant-' + scope, 'revoke-' + scope):
            if args.agent not in data['agents']:
                parser.error('Unknown agent')
            actions = set(data['agents'][args.agent]['actions'])
            if args.action.startswith('grant-'):
                actions.add(permission)
            else:
                actions.discard(permission)
            data['agents'][args.agent]['actions'] = sorted(actions)
            registry.save(data)
            print('Optional permission updated; no Telegram message or file was sent.')
            return
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', args.agent) or not 1 <= args.expires_days <= 365:
        parser.error('Invalid agent or lifetime')
    if args.agent in data['agents']:
        parser.error('Agent already exists; use a new identity or revoke it first.')
    if any(x == 0 for x in args.chat_id) or any(x <= 0 for x in args.contact_id) or any(x >= 0 for x in args.poll_chat_id):
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
            'include_phones': args.include_phones,
            'poll_write_saved': args.poll_saved, 'poll_write_all_chats': args.poll_all_chats,
            'poll_write_chat_ids': sorted(set(args.poll_chat_id)),
            'actions': sorted(READ_ACTIONS | ({SAVED_TEXT_ACTION} if args.send_saved_text else set()) |
                              {name for name in OPTIONAL_ACTIONS if getattr(args, name.replace('-', '_'), False)})}
        registry.save(data)
    except BaseException:
        token_path.unlink(missing_ok=True)
        raise
    print(json.dumps({'agent': args.agent, 'token_file': str(token_path),
                      'raw_token_printed': False, 'read_only': not (args.send_saved_text or args.send_saved_file or args.create_polls or args.close_own_polls)}))


if __name__ == '__main__':
    main()
