"""Offline-only BotFather protocol model. It is NOT registered as an MCP tool.

No Telegram transport, consent verifier, durable claims, or production vault is
provided here. Those are required before any real credential can be created.
"""
import re

from .poll_common import valid_request_id
from .sensitive_peer import BOTFATHER_ID, redact_tokens

_USERNAME = re.compile(r'[A-Za-z][A-Za-z0-9_]{4,31}\Z')
_TOKEN = re.compile(r'[1-9][0-9]{4,14}:[A-Za-z0-9_-]{30,}\Z')
_REF = re.compile(r'[A-Za-z0-9_-]{16,128}\Z')


def newbot_plan(name, username, request_id):
    valid_request_id(request_id)
    if (not isinstance(name, str) or not name.strip() or len(name.encode('utf-16-le')) // 2 > 64
            or redact_tokens(name) != name):
        raise ValueError('invalid_bot_name')
    if (not isinstance(username, str) or not _USERNAME.fullmatch(username)
            or not username.lower().endswith('bot')):
        raise ValueError('invalid_bot_username')
    return {'name': name, 'username': username, 'request_id': request_id}


class NewBotProtocol:
    """A fail-closed state model driven only by a trusted local adapter.

    The adapter must authenticate the peer, associate each reply to the exact
    sent request, classify known prompts without logging raw replies, claim a
    durable request before dispatch, and provide a separate secure vault.
    An unexpected response ends the attempt with no automatic retries.
    """
    def __init__(self, name, username, request_id, vault):
        self.plan = newbot_plan(name, username, request_id)
        self.vault = vault
        self.phase = 'ready_newbot'
        self.sent_id = None
        self.last_response_id = 0
        self.token_ref = None

    def command(self):
        values = {'ready_newbot': '/newbot', 'ready_name': self.plan['name'],
                  'ready_username': self.plan['username']}
        if self.phase not in values:
            raise RuntimeError('newbot_command_not_ready')
        return values[self.phase]

    def sent(self, message_id):
        transitions = {'ready_newbot': 'waiting_name', 'ready_name': 'waiting_username',
                       'ready_username': 'waiting_result'}
        if self.phase not in transitions or type(message_id) is not int or message_id <= 0:
            self.phase = 'stopped_uncertain'; return
        self.phase = transitions[self.phase]
        self.sent_id = message_id

    def response(self, *, sender_id, chat_id, message_id, response_to, kind, secret=None):
        if (self.phase not in ('waiting_name', 'waiting_username', 'waiting_result')
                or sender_id != BOTFATHER_ID or chat_id != BOTFATHER_ID
                or type(message_id) is not int or message_id <= self.last_response_id
                or response_to != self.sent_id):
            self.phase = 'stopped_uncertain'; return self.status()
        self.last_response_id = message_id
        if self.phase == 'waiting_name' and kind == 'name_prompt' and secret is None:
            self.phase = 'ready_name'
        elif self.phase == 'waiting_username' and kind == 'username_prompt' and secret is None:
            self.phase = 'ready_username'
        elif self.phase == 'waiting_result' and kind == 'created' and isinstance(secret, str) and _TOKEN.fullmatch(secret):
            try:
                ref = self.vault.store(secret)
                if not isinstance(ref, str) or not _REF.fullmatch(ref):
                    raise ValueError('invalid_reference')
                self.token_ref = ref
                self.phase = 'stored'
            except Exception:
                self.phase = 'stopped_uncertain'
        else:
            self.phase = 'stopped_uncertain'
        return self.status()

    def status(self):
        result = {'request_id': self.plan['request_id'], 'phase': self.phase}
        if self.phase == 'stored': result['token_ref'] = self.token_ref
        return result
