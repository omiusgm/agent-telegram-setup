"""Fixed three-step adapter; all replies and credentials are synthetic."""
import asyncio
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
import sys

from telethon.tl import functions, types

from telegram_setup.agents import AccessDenied
from telegram_setup import agents
from telegram_setup.newbot_worker import NewBotWorker
from telegram_setup.sensitive_peer import BOTFATHER_ID
from test_agent_gateway import RegistryFixture
from test_sensitive_peer import TOKEN

NAME, USERNAME, REQUEST = 'Synthetic', 'SyntheticLevBot', 'newbot-test-001'
NAME_PROMPT = 'Alright, a new bot. How are we going to call it? Please choose a name for your bot.'
USERNAME_PROMPT = "Good. Now let's choose a username for your bot. It must end in bot."
SUCCESS = 'Done! Congratulations on your new bot. You will find it at t.me/' + USERNAME + '. HTTP API: ' + TOKEN


class Barrier:
    def __init__(self): self.active = False
    def enter(self):
        if self.active: raise RuntimeError('busy')
        self.active = True
    def exit(self): self.active = False


class Conversation:
    def __init__(self, client): self.client = client
    async def __aenter__(self): return self
    async def __aexit__(self, *_): return False
    async def send_message(self, text, parse_mode):
        self.client.sent.append(text)
        if self.client.fail_send_at == len(self.client.sent): raise TimeoutError('synthetic private data')
        return NS(id=10 + 2 * (len(self.client.sent) - 1))
    async def get_response(self, sent, timeout):
        index = len(self.client.sent) - 1
        if self.client.outside_at == index + 1:
            await self.client.handler(NS(chat_id=BOTFATHER_ID, out=True))
        text = self.client.responses[index]
        return NS(chat_id=BOTFATHER_ID, sender_id=self.client.sender, id=sent.id + 1,
                  out=False, reply_to=None, raw_text=text)


class Client:
    def __init__(self):
        self.sent, self.calls = [], []
        self.responses = [NAME_PROMPT, USERNAME_PROMPT, SUCCESS]
        self.sender = BOTFATHER_ID; self.fail_send_at = 0; self.outside_at = 0
        self.handler = None; self.verify_owned = True; self.target_verified = True
    async def get_me(self): self.calls.append('get_me'); return NS(bot=False, id=1)
    async def get_entity(self, peer):
        self.calls.append('get_entity')
        assert peer == 'BotFather'
        return types.User(id=BOTFATHER_ID, access_hash=1, bot=True, verified=self.target_verified, username='BotFather')
    async def __call__(self, request, **kwargs):
        assert isinstance(request, functions.bots.GetAdminedBotsRequest)
        assert kwargs == {'flood_sleep_threshold': 0}
        self.calls.append('get_owned_bots')
        if len(self.sent) == 3 and self.verify_owned:
            return [types.User(id=123456789, access_hash=123, bot=True, username=USERNAME, first_name=NAME)]
        return []
    def add_event_handler(self, handler, event): self.handler = handler
    def remove_event_handler(self, handler, event): self.handler = None
    def conversation(self, entity, **kwargs):
        assert entity.id == BOTFATHER_ID
        assert kwargs['exclusive'] is True and kwargs['max_messages'] == 10
        return Conversation(self)
    async def get_messages(self, *args, **kwargs): raise AssertionError('history must never be read')


class NewBotWorkerTests(RegistryFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_registry(); self.data['agents']['a']['actions'].append('create-owned-bot'); self.registry.save(self.data)
        self.client = Client(); self.barrier = Barrier(); self.root = Path(self.temporary.name)
        self.worker = NewBotWorker(self.client, self.registry, self.root, barrier=self.barrier, timeout=1)
        self.approve()
    def tearDown(self): self.teardown_registry()
    def approve(self, name=NAME, username=USERNAME, request=REQUEST):
        self.worker.approvals.issue('a', name, username, request)

    async def test_exact_approved_flow_reuses_client_stores_token_and_verifies_ownership(self):
        with patch('telegram_setup.newbot_worker.logging_is_quiet', return_value=True):
            result = await self.worker.create('a', NAME, USERNAME, REQUEST)
            calls = list(self.client.calls)
            replay = await self.worker.create('a', NAME, USERNAME, REQUEST)
        self.assertEqual(result['status'], 'created_verified')
        self.assertEqual(result['bot_id'], 123456789)
        self.assertEqual(self.client.sent, ['/newbot', NAME, USERNAME])
        self.assertEqual(self.client.calls, calls)
        self.assertEqual(replay, result); self.assertFalse(self.barrier.active)
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertEqual(self.worker.vault.load_for_broker(result['token_ref']), TOKEN)
        for directory in (self.root / 'newbot-approvals', self.root / 'newbot-claims'):
            for path in directory.iterdir():
                if path.is_file(): self.assertNotIn(TOKEN, path.read_text())

    async def test_unexpected_prompt_stops_and_restart_never_resends(self):
        self.client.responses[0] = 'CAPTCHA: do something else'
        with patch('telegram_setup.newbot_worker.logging_is_quiet', return_value=True):
            result = await self.worker.create('a', NAME, USERNAME, REQUEST)
            replay = await self.worker.create('a', NAME, USERNAME, REQUEST)
        self.assertEqual(result['status'], 'uncertain_do_not_retry')
        self.assertEqual(replay, result); self.assertEqual(self.client.sent, ['/newbot'])
        self.assertNotIn('token_ref', result); self.assertIsNone(self.client.handler)

    async def test_other_device_interference_stops_before_next_command(self):
        self.client.outside_at = 1
        with patch('telegram_setup.newbot_worker.logging_is_quiet', return_value=True):
            result = await self.worker.create('a', NAME, USERNAME, REQUEST)
        self.assertEqual(result['status'], 'uncertain_do_not_retry')
        self.assertEqual(self.client.sent, ['/newbot'])

    async def test_spoofed_sender_bad_link_and_unverified_botfather_never_succeed(self):
        for failure in ('sender', 'link', 'verified'):
            with self.subTest(failure=failure):
                # Fresh temporary scope and approval per scenario.
                sub = self.root / failure
                worker = NewBotWorker(Client(), self.registry, sub, barrier=Barrier(), timeout=1)
                worker.approvals.issue('a', NAME, USERNAME, REQUEST)
                if failure == 'sender': worker.client.sender = 222
                if failure == 'link': worker.client.responses[2] = SUCCESS.replace(USERNAME, 'OtherFixtureBot')
                if failure == 'verified': worker.client.target_verified = False
                with patch('telegram_setup.newbot_worker.logging_is_quiet', return_value=True):
                    if failure == 'verified':
                        with self.assertRaises(AccessDenied): await worker.create('a', NAME, USERNAME, REQUEST)
                        self.assertEqual(worker.client.sent, [])
                    else:
                        result = await worker.create('a', NAME, USERNAME, REQUEST)
                        self.assertEqual(result['status'], 'uncertain_do_not_retry')
                        self.assertNotIn(TOKEN, json.dumps(result))

    async def test_unverified_ownership_and_send_timeout_never_expose_token_or_retry(self):
        for failure in ('ownership', 'timeout'):
            sub = self.root / failure
            worker = NewBotWorker(Client(), self.registry, sub, barrier=Barrier(), timeout=1)
            worker.approvals.issue('a', NAME, USERNAME, REQUEST)
            if failure == 'ownership': worker.client.verify_owned = False
            else: worker.client.fail_send_at = 3
            with patch('telegram_setup.newbot_worker.logging_is_quiet', return_value=True):
                result = await worker.create('a', NAME, USERNAME, REQUEST)
                replay = await worker.create('a', NAME, USERNAME, REQUEST)
            self.assertEqual(result['status'], 'uncertain_do_not_retry')
            self.assertEqual(replay, result); self.assertEqual(len(worker.client.sent), 3)
            self.assertNotIn(TOKEN, json.dumps(result)); self.assertNotIn('token_ref', result)

    async def test_no_exact_approval_or_permission_and_debug_logging_prevent_sends(self):
        with patch('telegram_setup.newbot_worker.logging_is_quiet', return_value=True):
            with self.assertRaises((FileNotFoundError, PermissionError)):
                await self.worker.create('a', 'Different', USERNAME, 'newbot-other-1')
        self.assertEqual(self.client.sent, [])
        with patch('telegram_setup.newbot_worker.logging_is_quiet', return_value=False):
            with self.assertRaises(PermissionError): await self.worker.create('a', NAME, USERNAME, REQUEST)
        self.data['agents']['a']['actions'].remove('create-owned-bot'); self.registry.save(self.data)
        with self.assertRaises(AccessDenied): await self.worker.create('a', NAME, USERNAME, REQUEST)
        self.assertEqual(self.client.sent, [])

    async def test_process_cancellation_after_dispatch_never_recreates_the_bot(self):
        dispatched = asyncio.Event()
        class HangingConversation(Conversation):
            async def send_message(inner, text, parse_mode):
                if len(inner.client.sent) == 2:
                    inner.client.sent.append(text); dispatched.set(); await asyncio.Event().wait()
                return await super(HangingConversation, inner).send_message(text, parse_mode)
        self.client.conversation = lambda entity, **kwargs: HangingConversation(self.client)
        with patch('telegram_setup.newbot_worker.logging_is_quiet', return_value=True):
            task = asyncio.create_task(self.worker.create('a', NAME, USERNAME, REQUEST))
            await dispatched.wait(); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            replay = await self.worker.create('a', NAME, USERNAME, REQUEST)
        self.assertEqual(replay['status'], 'uncertain_do_not_retry')
        self.assertEqual(self.client.sent, ['/newbot', NAME, USERNAME])
        self.assertFalse(self.barrier.active)

    async def test_delegated_mode_has_no_operator_assertion_and_does_not_claim_human_verification(self):
        self.data['agents']['a']['bot_create_mode'] = 'delegated'; self.registry.save(self.data)
        worker = NewBotWorker(Client(), self.registry, self.root / 'delegated', barrier=Barrier(), timeout=1)
        with patch('telegram_setup.newbot_worker.logging_is_quiet', return_value=True):
            result = await worker.create('a', NAME, USERNAME, REQUEST)
            replay = await worker.create('a', NAME, USERNAME, REQUEST)
        self.assertEqual(result['status'], 'created_verified'); self.assertEqual(replay, result)
        self.assertEqual(result['authorization_mode'], 'delegated')
        self.assertFalse(result['owner_identity_verified'])
        self.assertFalse((self.root / 'delegated' / 'newbot-approvals').exists())
        self.assertEqual(worker.client.sent, ['/newbot', NAME, USERNAME])

    async def test_revoking_delegated_mode_mid_conversation_stops_without_retry(self):
        self.data['agents']['a']['bot_create_mode'] = 'delegated'; self.registry.save(self.data)
        worker = NewBotWorker(Client(), self.registry, self.root / 'revocation', barrier=Barrier(), timeout=1)
        class RevokingConversation(Conversation):
            async def get_response(inner, sent, timeout):
                value = await super(RevokingConversation, inner).get_response(sent, timeout)
                if len(inner.client.sent) == 1:
                    self.data['agents']['a'].pop('bot_create_mode'); self.registry.save(self.data)
                return value
        worker.client.conversation = lambda entity, **kwargs: RevokingConversation(worker.client)
        with patch('telegram_setup.newbot_worker.logging_is_quiet', return_value=True):
            result = await worker.create('a', NAME, USERNAME, REQUEST)
        self.assertEqual(result['status'], 'uncertain_do_not_retry')
        self.assertEqual(worker.client.sent, ['/newbot'])
        self.assertFalse(result['owner_identity_verified'])

    def test_delegated_mode_cli_is_separate_revocable_and_default_operator(self):
        self.assertNotIn('bot_create_mode', self.registry.profile('a'))
        command = ['agents', '--registry', str(self.registry.path), 'grant-bot-delegated', 'a']
        with patch.object(sys, 'argv', command), contextlib.redirect_stdout(io.StringIO()): agents.main()
        self.assertEqual(self.registry.profile('a')['bot_create_mode'], 'delegated')
        command[-2] = 'revoke-bot-delegated'
        with patch.object(sys, 'argv', command), contextlib.redirect_stdout(io.StringIO()): agents.main()
        self.assertNotIn('bot_create_mode', self.registry.profile('a'))
        self.data['agents']['a']['actions'].remove('create-owned-bot'); self.registry.save(self.data)
        command[-2] = 'grant-bot-delegated'
        with patch.object(sys, 'argv', command), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit): agents.main()
        self.assertNotIn('bot_create_mode', self.registry.profile('a'))

    def test_revoking_creation_clears_delegation_before_future_regrant(self):
        self.data['agents']['a']['bot_create_mode'] = 'delegated'; self.registry.save(self.data)
        for action in ('revoke-bot-create', 'grant-bot-create'):
            args = ['agents', '--registry', str(self.registry.path), action, 'a']
            with patch.object(sys, 'argv', args), contextlib.redirect_stdout(io.StringIO()): agents.main()
        profile = self.registry.profile('a')
        self.assertIn('create-owned-bot', profile['actions'])
        self.assertNotIn('bot_create_mode', profile)
