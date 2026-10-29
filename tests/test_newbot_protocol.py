"""Offline protocol model: synthetic responses and synthetic credentials only."""
import json
import unittest

from telegram_setup.newbot_protocol import NewBotProtocol, newbot_plan
from telegram_setup.sensitive_peer import BOTFATHER_ID
from test_sensitive_peer import TOKEN


class Vault:
    def __init__(self): self.values = []
    def store(self, token): self.values.append(token); return 'opaque_reference_1234567890'


def event(proto, mid, response_to, kind, **kwargs):
    return proto.response(sender_id=BOTFATHER_ID, chat_id=BOTFATHER_ID,
                          message_id=mid, response_to=response_to, kind=kind, **kwargs)


class NewBotTests(unittest.TestCase):
    def test_happy_path_stores_synthetic_secret_and_returns_only_reference(self):
        vault = Vault(); proto = NewBotProtocol('Лев', 'SyntheticLevBot', 'newbot-test-001', vault)
        self.assertEqual(proto.command(), '/newbot'); proto.sent(10)
        self.assertEqual(event(proto, 11, 10, 'name_prompt')['phase'], 'ready_name')
        self.assertEqual(proto.command(), 'Лев'); proto.sent(12)
        self.assertEqual(event(proto, 13, 12, 'username_prompt')['phase'], 'ready_username')
        self.assertEqual(proto.command(), 'SyntheticLevBot'); proto.sent(14)
        result = event(proto, 15, 14, 'created', secret=TOKEN)
        self.assertEqual(result['phase'], 'stored'); self.assertNotIn(TOKEN, json.dumps(result))
        self.assertEqual(vault.values, [TOKEN]); self.assertNotIn(TOKEN, repr(proto.__dict__))

    def test_unexpected_reply_sender_or_wrong_request_stops(self):
        for change in ({'kind': 'captcha'}, {'sender_id': 5}, {'response_to': 77}, {'chat_id': 8}):
            proto = NewBotProtocol('Лев', 'SyntheticLevBot', 'newbot-test-001', Vault())
            proto.sent(10)
            value = {'sender_id': BOTFATHER_ID, 'chat_id': BOTFATHER_ID, 'message_id': 11,
                     'response_to': 10, 'kind': 'name_prompt', **change}
            self.assertEqual(proto.response(**value)['phase'], 'stopped_uncertain')
            with self.assertRaises(RuntimeError): proto.command()

    def test_vault_error_and_unrecognized_secret_never_echo(self):
        class Broken:
            def store(self, secret): raise RuntimeError(secret)
        proto = NewBotProtocol('Лев', 'SyntheticLevBot', 'newbot-test-001', Broken())
        proto.sent(10); event(proto, 11, 10, 'name_prompt'); proto.sent(12)
        event(proto, 13, 12, 'username_prompt'); proto.sent(14)
        result = event(proto, 15, 14, 'created', secret=TOKEN)
        self.assertEqual(result['phase'], 'stopped_uncertain'); self.assertNotIn(TOKEN, json.dumps(result))

    def test_inputs_are_bounded_and_username_is_explicit(self):
        for username in ('lev', ' lev_bot', 'a_bot?', 'bot', '1helloBot', 'valid_but_no_suffix'):
            with self.assertRaises(ValueError): newbot_plan('Лев', username, 'newbot-test-001')
        with self.assertRaises(ValueError): newbot_plan(TOKEN, 'SyntheticLevBot', 'newbot-test-001')
