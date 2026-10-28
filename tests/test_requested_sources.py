import importlib.util
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_setup import source_intents as intents
from telegram_setup import requested_sources
from telethon import errors, functions, types as tgtypes


class IntentsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        os.chmod(self.home, 0o700)

    def tearDown(self):
        self.temp.cleanup()

    def test_link_normalization_and_rejection(self):
        self.assertEqual(intents.normalize_source('https://t.me/SomeChannel/314'), 'https://t.me/somechannel')
        self.assertEqual(intents.normalize_source('https://t.me/joinchat/abc_DEF12345'), 'https://t.me/+abc_DEF12345')
        for s in ('https://t.me.evil.test/hello', 'https://' + 'user' + '@' + 't.me/hello', 'https://t.me/c/123',
                  'https://t.me/hello/nope', 'https://t.me/+short'):
            with self.subTest(s=s), self.assertRaises(ValueError):
                intents.normalize_source(s)

    def test_fresh_exact_intent_only_and_once(self):
        intents.record(self.home, '@rightchannel', '123', now=100)
        with self.assertRaises(ValueError):
            intents.claim(self.home, '@otherchannel', now=101)
        self.assertEqual(intents.claim(self.home, 'https://t.me/rightchannel', now=101), '123')
        with self.assertRaises(ValueError):
            intents.claim(self.home, '@rightchannel', now=101)
        intents.finish(self.home, '@rightchannel', '123', 'joined')
        self.assertEqual((self.home / 'source-intents.sqlite3').stat().st_mode & 0o777, 0o600)
        intents.record(self.home, '@expiredchannel', '124', now=100)
        with self.assertRaises(ValueError):
            intents.claim(self.home, '@expiredchannel', now=100+43201)

    def test_floodwait_deferred(self):
        intents.record(self.home, '@rightchannel', '123')
        msg = intents.claim(self.home, '@rightchannel')
        intents.defer(self.home, '@rightchannel', msg, 60)
        with self.assertRaises(ValueError):
            intents.claim(self.home, '@rightchannel')


@unittest.skipUnless((Path(__file__).resolve().parents[2] / 'outputs/telegram-ezh-sources-20261028/plugin/__init__.py').is_file(), 'Operator-specific ingress is not part of the public package')
class IngressTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[2] / 'outputs/telegram-ezh-sources-20261028/plugin/__init__.py'
        spec = importlib.util.spec_from_file_location('source_hook_test', path)
        cls.hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.hook)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'; self.home.mkdir(mode=0o700)
        self.owner = self.root / 'owner.json'; self.owner.write_text('{"owner_id": 12345}')
        os.chmod(self.owner, 0o600)
        self.paths = patch.multiple(self.hook, HOME=self.home, OWNER=self.owner)
        self.paths.start()

    def tearDown(self):
        self.paths.stop(); self.temp.cleanup()

    def event(self, user='12345', chat='12345', kind='dm', text='Посмотри https://t.me/newsroom', origin=None):
        source = types.SimpleNamespace(platform=types.SimpleNamespace(value='telegram'), user_id=user,
                                       chat_id=chat, chat_type=kind)
        return types.SimpleNamespace(source=source, internal=False, raw_message=types.SimpleNamespace(forward_origin=origin),
                                     message_id='42', text=text)

    def test_only_owners_direct_dm_captures(self):
        self.hook.capture(self.event(user='222'))
        self.hook.capture(self.event(kind='group'))
        with self.assertRaises(ValueError): intents.claim(self.home, '@newsroom')
        self.hook.capture(self.event())
        self.assertEqual(intents.claim(self.home, '@newsroom'), '42')

    def test_forwarded_post_approves_origin_not_embedded_text(self):
        origin = types.SimpleNamespace(chat=types.SimpleNamespace(username='actualsource', type='channel'))
        result = self.hook.capture(self.event(text='https://t.me/untrustedlink', origin=origin))
        self.assertIn('actualsource', result['text'])
        with self.assertRaises(ValueError): intents.claim(self.home, '@untrustedlink')
        self.assertEqual(intents.claim(self.home, '@actualsource'), '42')


class JoinTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        os.chmod(self.home, 0o700)
        self.entity = tgtypes.Channel(id=123, title='Example', photo=None, date=None,
                                      username='newsroom', megagroup=True)
        self.client = types.SimpleNamespace()
        self.client.get_entity = self.get_entity
        self.client.get_permissions = self.get_permissions

        class Client:
            async def get_entity(_, username): return await self.get_entity(username)
            async def get_permissions(_, entity, user): return await self.get_permissions(entity, user)
            async def __call__(_, req): return await self.request(req)
        self.client = Client()
        class MCP:
            def tool(_, **kwargs):
                def decor(fn): self.join = fn; return fn
                return decor
        runtime = types.SimpleNamespace(mcp=MCP(), ToolAnnotations=lambda **kw: kw,
                                        with_account=lambda **kw: lambda fn: fn,
                                        get_client=lambda account: self.client,
                                        get_marked_id=lambda entity: -100123)
        self.patch = patch.object(requested_sources, 'private_dir', return_value=self.home)
        self.patch.start(); requested_sources.register_requested_source(runtime)
        self.requests = []; self.fail = None

    def tearDown(self):
        self.patch.stop(); self.temp.cleanup()

    async def get_entity(self, username): return self.entity
    async def get_permissions(self, entity, user): return object()
    async def request(self, req):
        self.requests.append(req)
        if self.fail: raise self.fail
        return object()

    async def test_no_join_without_exact_owner_intent(self):
        result = await self.join('https://t.me/newsroom')
        self.assertEqual(result['status'], 'owner_request_required')
        self.assertEqual(self.requests, [])

    async def test_join_once_and_verify_membership(self):
        intents.record(self.home, '@newsroom', 123)
        result = await self.join('https://t.me/newsroom')
        self.assertEqual(result['status'], 'joined')
        self.assertEqual(result['chat_id'], -100123)
        self.assertIsInstance(self.requests[0], functions.channels.JoinChannelRequest)
        self.assertEqual((await self.join('@newsroom'))['status'], 'owner_request_required')

    async def test_floodwait_is_deferred_and_never_immediately_retried(self):
        intents.record(self.home, '@newsroom', 123)
        self.fail = errors.FloodWaitError(request=None, capture=10)
        result = await self.join('@newsroom')
        self.assertEqual(result['status'], 'rate_limited')
        self.assertEqual(result['retry_after_seconds'], 10)
        self.assertEqual((await self.join('@newsroom'))['status'], 'owner_request_required')
        self.assertEqual(len(self.requests), 1)

    async def test_non_channel_never_joined(self):
        self.entity = tgtypes.User(id=5)
        intents.record(self.home, '@newsroom', 123)
        self.assertEqual((await self.join('@newsroom'))['status'], 'not_a_channel_or_group')
        self.assertEqual(self.requests, [])

    async def test_existing_invitation_does_not_join_twice(self):
        async def request(req):
            self.requests.append(req)
            if isinstance(req, functions.messages.CheckChatInviteRequest):
                return tgtypes.ChatInviteAlready(chat=self.entity)
            raise AssertionError('unexpected join')
        self.request = request
        intents.record(self.home, 'https://t.me/+abc_DEF12345', 123)
        result = await self.join('https://t.me/+abc_DEF12345')
        self.assertEqual(result['status'], 'already_joined')
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn('abc_DEF12345', str(result))

    async def test_invitation_request_waiting_for_admin_is_not_reported_joined(self):
        async def request(req):
            self.requests.append(req)
            if isinstance(req, functions.messages.CheckChatInviteRequest): return object()
            raise errors.InviteRequestSentError(request=req)
        self.request = request
        intents.record(self.home, 'https://t.me/+abc_DEF12345', 123)
        result = await self.join('https://t.me/+abc_DEF12345')
        self.assertEqual(result, {'status': 'approval_pending'})


if __name__ == '__main__':
    unittest.main()
