"""Synthetic owner-only notification tests; no live bot, Telegram, or HTTP."""
import json
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from telethon.tl import functions, types

from telegram_setup.agents import AccessDenied
from telegram_setup.client import text_payload
from telegram_setup.gateway import create_gateway
from telegram_setup.newbot_store import NewBotClaims, OperatorApprovals
from telegram_setup.newbot_token_files import FileBotTokenStore
from telegram_setup.owner_bot_notifications import OwnerBotNotifier, bot_api_send
from telegram_setup.owner_bot_notification_gateway import OwnerBotNotificationGateway
from telegram_setup.reader_gateway import ReaderGateway
from test_agent_gateway import RegistryFixture
from test_sensitive_peer import TOKEN

BOT_ID, OWNER_ID = 123456789, 101
BOT_MESSAGE_ID, OWNER_MESSAGE_ID, BASELINE_ID, BOT_DATE = 55, 9001, 8000, 1793320690
TEXT, REQUEST = 'Synthetic owner notification', 'bot-notify-001'


class Client:
    def __init__(self):
        self.bot_owned = True; self.readback = True; self.reads = []
        self.baseline_error = False; self.history_variant = 'single';self.expected_text=TEXT;self.history_calls=0
    async def get_me(self): return NS(id=OWNER_ID, bot=False)
    async def __call__(self, request, **kwargs):
        assert isinstance(request, functions.bots.GetAdminedBotsRequest)
        assert kwargs == {'flood_sleep_threshold': 0}
        return [types.User(id=BOT_ID, access_hash=1, bot=True, username='SyntheticLevBot')] if self.bot_owned else []
    async def get_messages(self, bot, **kwargs):
        self.reads.append((bot.id, kwargs))
        if kwargs == {'limit':1}:
            if self.baseline_error:raise RuntimeError('synthetic baseline failure')
            return [NS(id=BASELINE_ID, chat_id=BOT_ID)]
        assert kwargs == {'limit':51, 'min_id':BASELINE_ID}
        self.history_calls+=1
        if not self.readback:return []
        def msg(ident=OWNER_MESSAGE_ID, **changes):
            values={'id':ident,'chat_id':BOT_ID,'sender_id':BOT_ID,'out':False,'raw_text':self.expected_text,
                    'date':datetime.fromtimestamp(BOT_DATE,timezone.utc)};values.update(changes);return NS(**values)
        if self.history_variant=='duplicate':return [msg(),msg(OWNER_MESSAGE_ID+1)]
        if self.history_variant=='wrong_sender':return [msg(sender_id=777)]
        if self.history_variant=='wrong_peer':return [msg(chat_id=777)]
        if self.history_variant=='outgoing':return [msg(out=True)]
        if self.history_variant=='wrong_date':return [msg(date=datetime.fromtimestamp(BOT_DATE-1,timezone.utc))]
        if self.history_variant=='wrong_text':return [msg(raw_text='other')]
        if self.history_variant=='before_baseline':return [msg(BASELINE_ID)]
        if self.history_variant=='over_limit':return [msg(OWNER_MESSAGE_ID+i) for i in range(51)]
        if self.history_variant=='timeout':raise TimeoutError('synthetic history timeout')
        if self.history_variant=='appears_late' and self.history_calls==1:return []
        if self.history_variant=='candidate_changes' and self.history_calls>1:return [msg(OWNER_MESSAGE_ID+1)]
        return [msg()]


class NotifierTests(RegistryFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_registry(); self.data['agents']['a']['actions'].append('notify-owner-via-bot')
        self.data['agents']['a']['bot_notify_ids'] = [BOT_ID]; self.registry.save(self.data)
        self.root = Path(self.temporary.name); self.client = Client(); self.sent = []
        approvals = OperatorApprovals(self.root / 'newbot-approvals')
        approvals.issue('a', 'Synthetic', 'SyntheticLevBot', 'newbot-test-001')
        with NewBotClaims(self.root / 'newbot-claims') as claims:
            claim, _ = claims.begin('a', 'Synthetic', 'SyntheticLevBot', 'newbot-test-001', approvals)
            ref = FileBotTokenStore(self.root / 'bot-tokens').for_intent(claim['token_intent']).store(TOKEN)
            assert ref == claim['reserved_ref']
            claims.finish_verified(claim, BOT_ID, 'SyntheticLevBot')
        def transport(secret, owner, bot, text):
            self.sent.append((secret, owner, bot, text))
            self.client.expected_text=text
            return {'status':'accepted_by_telegram','bot_message_id':BOT_MESSAGE_ID,'bot_date_unix':BOT_DATE}
        self.notifier = OwnerBotNotifier(self.client, self.registry, self.root, transport=transport)
        self.pause = patch('telegram_setup.owner_bot_notifications._HISTORY_PAUSE',0);self.pause.start()

    def tearDown(self): self.pause.stop();self.teardown_registry()

    async def test_only_owner_and_owned_bot_are_used_and_readback_verified(self):
        result = await self.notifier.send('a', BOT_ID, TEXT, REQUEST)
        replay = await self.notifier.send('a', BOT_ID, TEXT, REQUEST)
        self.assertEqual(result['status'], 'delivered_verified'); self.assertEqual(replay, result)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1:], (OWNER_ID, BOT_ID, TEXT))
        self.assertNotIn(TOKEN, json.dumps(result)); self.assertNotIn(TEXT, json.dumps(result))
        self.assertEqual(result['bot_message_id'],BOT_MESSAGE_ID)
        self.assertEqual(result['owner_message_id'],OWNER_MESSAGE_ID)
        self.assertNotEqual(result['bot_message_id'],result['owner_message_id'])
        self.assertEqual(result['message_id'],OWNER_MESSAGE_ID)
        self.assertEqual(self.client.reads,[(BOT_ID,{'limit':1}),
            (BOT_ID,{'limit':51,'min_id':BASELINE_ID}),(BOT_ID,{'limit':51,'min_id':BASELINE_ID})])

    async def test_acceptance_without_owner_readback_is_not_delivery_or_resent(self):
        self.client.readback = False
        result = await self.notifier.send('a', BOT_ID, TEXT, REQUEST)
        replay = await self.notifier.send('a', BOT_ID, TEXT, REQUEST)
        other_id = await self.notifier.send('a', BOT_ID, TEXT, 'bot-notify-002')
        self.assertEqual(result['status'], 'accepted_unconfirmed')
        self.assertFalse(result['readback_verified']); self.assertEqual(replay, result)
        self.assertEqual(result['bot_message_id'],BOT_MESSAGE_ID);self.assertNotIn('owner_message_id',result)
        self.assertEqual(other_id['status'], 'uncertain_do_not_retry'); self.assertEqual(len(self.sent), 1)

    async def test_ambiguous_or_mismatched_history_never_claims_delivery_or_resends(self):
        for variant in ('duplicate','wrong_sender','wrong_peer','outgoing','wrong_date','wrong_text',
                        'before_baseline','over_limit','timeout','candidate_changes'):
            with self.subTest(variant=variant):
                self.client.history_variant=variant;self.client.history_calls=0
                req='bot-notify-'+variant.replace('_','-')
                result=await self.notifier.send('a',BOT_ID,TEXT+' '+variant,req)
                self.assertEqual(result['status'],'accepted_unconfirmed')
                self.assertFalse(result['readback_verified'])
                self.assertNotIn('owner_message_id',result)
                self.assertEqual((await self.notifier.send('a',BOT_ID,TEXT+' '+variant,req)),result)
        self.assertEqual(len(self.sent),10)

    async def test_late_matching_owner_message_can_be_verified_without_resending(self):
        self.client.history_variant='appears_late'
        result=await self.notifier.send('a',BOT_ID,TEXT,REQUEST)
        self.assertEqual(result['status'],'delivered_verified');self.assertEqual(result['owner_message_id'],OWNER_MESSAGE_ID)
        self.assertEqual(len(self.sent),1);self.assertEqual(self.client.history_calls,3)

    async def test_two_exact_matches_are_unconfirmed_and_block_new_request(self):
        self.client.history_variant='duplicate'
        result=await self.notifier.send('a',BOT_ID,TEXT,REQUEST)
        retry=await self.notifier.send('a',BOT_ID,TEXT,'bot-notify-002')
        self.assertEqual(result['status'],'accepted_unconfirmed')
        self.assertEqual(retry['status'],'uncertain_do_not_retry');self.assertEqual(len(self.sent),1)

    async def test_snapshot_failure_happens_before_any_send(self):
        self.client.baseline_error=True
        with self.assertRaises(RuntimeError):await self.notifier.send('a',BOT_ID,TEXT,REQUEST)
        self.assertEqual(self.sent,[])
        self.assertFalse((self.root/'bot-notification-receipts').exists())

    async def test_ungranted_or_no_longer_owned_bot_cannot_send(self):
        with self.assertRaises(AccessDenied): await self.notifier.send('a', 777, TEXT, REQUEST)
        self.client.bot_owned = False
        with self.assertRaises(AccessDenied): await self.notifier.send('a', BOT_ID, TEXT, REQUEST)
        self.assertEqual(self.sent, [])

    async def test_rate_limit_or_bot_unstarted_returns_only_fixed_error(self):
        self.notifier.transport = lambda *_: {'status': 'rejected_not_sent', 'error_code': 403,
                                              'raw': 'secret ' + TOKEN}
        result = await self.notifier.send('a', BOT_ID, TEXT, REQUEST)
        self.assertEqual(result['status'], 'rejected_not_sent'); self.assertEqual(result['error_code'], 403)
        self.assertNotIn(TOKEN, json.dumps(result))

    async def test_text_token_and_forged_target_are_rejected(self):
        with self.assertRaises(ValueError): await self.notifier.send('a', BOT_ID, TOKEN, REQUEST)
        with self.assertRaises(AccessDenied): await self.notifier.send('b', BOT_ID, TEXT, REQUEST)
        self.assertEqual(self.sent, [])

    async def test_gateway_has_separate_action_and_is_disabled_by_default(self):
        mcp = create_gateway(self.registry, 'a')
        self.assertNotIn('notify_owner_from_bot', {t.name for t in await mcp.list_tools()})
        with self.assertRaises(ValueError): create_gateway(self.registry, None, enable_bot_notifications=True)
        mcp = create_gateway(self.registry, 'a', enable_bot_notifications=True)
        self.assertIn('notify_owner_from_bot', {t.name for t in await mcp.list_tools()})
        class Bridge:
            available_tools = {'agent_notify_owner_via_bot'}
            async def call(_, name, args):
                return await self.notifier.send(args['agent_id'], args['bot_id'], args['text'], args['request_id'])
        api = ReaderGateway(self.registry, Bridge())
        result = await OwnerBotNotificationGateway(api).send('a', BOT_ID, TEXT, REQUEST)
        self.assertEqual(result['status'], 'delivered_verified'); self.assertNotIn(TOKEN, json.dumps(result))

    async def test_gateway_rejects_delivery_claim_without_distinct_namespace_evidence(self):
        class Bridge:
            available_tools={'agent_notify_owner_via_bot'}
            async def call(_,name,args):return {'request_id':args['request_id'],'bot_id':BOT_ID,
                'status':'delivered_verified','readback_verified':True,'message_id':BOT_MESSAGE_ID}
        api=ReaderGateway(self.registry,Bridge())
        result=await OwnerBotNotificationGateway(api).send('a',BOT_ID,TEXT,REQUEST)
        self.assertEqual(result['status'],'uncertain_do_not_retry');self.assertFalse(result['readback_verified'])

    async def test_real_stdio_disallows_arbitrary_recipient_and_returns_no_token(self):
        script = Path(__file__).with_name('offline_owner_bot_gateway.py')
        params = StdioServerParameters(command=sys.executable, args=[str(script), str(self.root), str(self.registry.path)])
        with open(os.devnull, 'w') as err:
            async with stdio_client(params, errlog=err) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    args = {'bot_id': BOT_ID, 'text': TEXT, 'request_id': REQUEST}
                    result = await session.call_tool('notify_owner_from_bot', args)
                    self.assertFalse(result.isError); payload=text_payload(result)
                    self.assertEqual(payload['status'],'delivered_verified')
                    self.assertEqual(payload['message_id'],OWNER_MESSAGE_ID)
                    self.assertEqual(payload['bot_message_id'],BOT_MESSAGE_ID)
                    self.assertNotIn(TOKEN, str(result))
                    spoof = await session.call_tool('notify_owner_from_bot', dict(args, chat_id=999))
                    self.assertTrue(spoof.isError); self.assertNotIn(TOKEN, str(spoof))


class TransportTests(unittest.TestCase):
    def fixture(self, payload, status=200, fail=False):
        class Connection:
            def __init__(self, host, port, **kwargs):
                assert host == 'api.telegram.org' and port == 443; self.debug = None
            def set_debuglevel(self, value): self.debug = value
            def request(self, method, path, body, headers):
                assert self.debug == 0 and method == 'POST' and path.endswith('/sendMessage')
                assert json.loads(body)['chat_id'] == OWNER_ID
                if fail: raise RuntimeError(TOKEN)
            def getresponse(self): return NS(status=status, read=lambda _: json.dumps(payload).encode())
            def close(self): pass
        with patch('telegram_setup.owner_bot_notifications.http.client.HTTPSConnection', Connection):
            return bot_api_send(TOKEN, OWNER_ID, BOT_ID, TEXT)

    def test_success_and_recipient_identity(self):
        message = {'ok': True, 'result': {'message_id': BOT_MESSAGE_ID, 'date': BOT_DATE, 'chat': {'id': OWNER_ID},
                                         'from': {'id': BOT_ID}, 'text': TEXT}}
        good = self.fixture(message)
        self.assertEqual(good, {'status':'accepted_by_telegram','bot_message_id':BOT_MESSAGE_ID,'bot_date_unix':BOT_DATE})
        message['result']['chat']['id'] = 999
        self.assertEqual(self.fixture(message)['status'], 'uncertain_do_not_retry')

    def test_missing_server_date_is_not_assumed_to_be_verified(self):
        message={'ok':True,'result':{'message_id':BOT_MESSAGE_ID,'chat':{'id':OWNER_ID},
                 'from':{'id':BOT_ID},'text':TEXT}}
        self.assertEqual(self.fixture(message)['status'],'uncertain_do_not_retry')

    def test_error_does_not_return_provider_description_or_token(self):
        out = self.fixture({'ok': False, 'error_code': 429, 'description': TOKEN,
                            'parameters': {'retry_after': 3}}, status=429)
        self.assertEqual(out, {'status': 'rejected_not_sent', 'error_code': 429, 'retry_after_seconds': 3})
        self.assertNotIn(TOKEN, json.dumps(out))
        out = self.fixture({}, fail=True)
        self.assertEqual(out, {'status': 'uncertain_do_not_retry'}); self.assertNotIn(TOKEN, json.dumps(out))
