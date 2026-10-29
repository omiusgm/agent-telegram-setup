"""Synthetic tests only; no real Telegram client or network calls."""
import asyncio
import hashlib
import json
import os
import contextlib
import subprocess
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import patch
import sys

from mcp.types import ToolAnnotations
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from telethon import functions, types as tg

from telegram_setup.agents import AccessDenied, AgentRegistry, READ_ACTIONS
from telegram_setup.poll_backend import register_poll_backend
from telegram_setup.poll_common import validate_poll
from telegram_setup.poll_gateway import PollGateway
from telegram_setup.reader_gateway import ReaderGateway
from telegram_setup.client import text_payload
from telegram_setup.gateway import create_gateway
from telegram_setup.permissions import save_settings


def row(actions=(), *, saved=False, targets=(), all_chats=True, chat_ids=(), poll_all=False):
    return {'enabled': True, 'token_sha256': hashlib.sha256(b'synthetic-token').hexdigest(),
            'expires_at':int(time.time())+3600,'actions':sorted(READ_ACTIONS|set(actions)),
            'all_chats':all_chats,'chat_ids':list(chat_ids),'all_contacts':False,'contact_ids':[],
            'include_phones':False,'poll_write_saved':saved,'poll_write_all_chats':poll_all,
            'poll_write_chat_ids':list(targets)}


class CommonTests(unittest.TestCase):
    def test_flags_quiz_and_options(self):
        self.assertEqual(validate_poll('Question?', ['Yes','No'], True, False, 1)['quiz_correct_index'],1)
        for value in [True,-1,2]:
            with self.assertRaises(ValueError): validate_poll('Q',['A','B'],True,False,value)
        with self.assertRaises(ValueError): validate_poll('Q',['A','B'],True,True,0)
        with self.assertRaises(ValueError): validate_poll('Q',['A','A'],True,False,None)
        with self.assertRaises(ValueError): validate_poll('Q',['A'],True,False,None)
        with self.assertRaises(ValueError): validate_poll('Q',['A','B'],'false',False,None)
        self.assertEqual(len(validate_poll('Q',[str(x) for x in range(12)],True,False,None)['options']),12)


class PollRegistryTests(unittest.TestCase):
    def test_optional_action_and_destination_grants_are_independent_and_revocable(self):
        with tempfile.TemporaryDirectory() as home:
            path=Path(home)/'agents.json'; registry=AgentRegistry(path)
            base=row({'send-saved-text'}); registry.save({'schema_version':1,'agents':{'a':base,'b':row()}})
            cmd=[sys.executable,'-m','telegram_setup.agents','--registry',str(path)]
            def call(*args,check=True):
                return subprocess.run(cmd+list(args),cwd=Path(__file__).resolve().parents[1],
                                      capture_output=True,text=True,check=check)
            call('grant-poll-create','a');call('grant-poll-saved','a')
            self.assertIn('create-polls',registry.profile('a')['actions'])
            self.assertTrue(registry.profile('a')['poll_write_saved'])
            self.assertNotIn('create-polls',registry.profile('b')['actions'])
            self.assertFalse(registry.profile('a')['poll_write_all_chats'])
            call('grant-poll-all-chats','a');self.assertTrue(registry.profile('a')['poll_write_all_chats'])
            bad=call('grant-poll-chat','a','--chat-id','123',check=False);self.assertNotEqual(bad.returncode,0)
            call('grant-poll-chat','a','--chat-id','-100123')
            self.assertEqual(registry.profile('a')['poll_write_chat_ids'],[-100123])
            call('revoke-poll-saved','a');call('revoke-poll-chat','a','--chat-id','-100123')
            call('revoke-poll-all-chats','a')
            call('revoke-poll-create','a')
            actual=registry.profile('a')
            self.assertFalse(actual['poll_write_saved']);self.assertFalse(actual['poll_write_all_chats'])
            self.assertEqual(actual['poll_write_chat_ids'],[])
            self.assertNotIn('create-polls',actual['actions']);self.assertIn('send-saved-text',actual['actions'])

    def test_malformed_all_chats_write_flag_fails_closed(self):
        with tempfile.TemporaryDirectory() as home:
            registry=AgentRegistry(Path(home)/'agents.json');bad=row({'create-polls'})
            bad['poll_write_all_chats']='true'
            registry.save({'schema_version':1,'agents':{'a':bad}})
            with self.assertRaises(AccessDenied):registry.load()


class FakeBackend:
    def __init__(self):
        self.available_tools = {'agent_read_poll','agent_create_poll','agent_close_own_poll'}
        self.polls = {}; self.sent = 0; self.closed = 0; self.uncertain = False; self.tamper = False
        self.owner_hook = None; self.pause = 0; self.reported_correct = None

    async def call(self, name, args):
        if name == 'get_me':
            if self.owner_hook: self.owner_hook()
            return {'type':'user','id':1001}
        if name == 'agent_create_poll':
            self.sent += 1
            if self.pause: await asyncio.sleep(self.pause)
            self.polls[(args['chat_id'],42)] = {'chat_id':args['chat_id'],'message_id':42,'poll_id':'987',
                'question':args['question'],'options':[{'index':i,'text':x,'votes':0} for i,x in enumerate(args['options'])],
                'anonymous':args['anonymous'],'multiple_choice':args['multiple_choice'],
                'quiz':args['quiz_correct_index'] is not None,'closed':False,'owner_created':True,'total_voters':0}
            if self.reported_correct is not None:
                self.polls[(args['chat_id'],42)]['options'][self.reported_correct]['correct']=True
            if self.uncertain: raise TimeoutError()
            return {'status':'delivered_verified','chat_id':args['chat_id'],'message_id':42,
                    'poll_id':'987','readback_verified':True}
        if name == 'agent_read_poll':
            value = dict(self.polls[(args['chat_id'],args['message_id'])])
            if self.tamper: value['question'] = 'different'
            return value
        if name == 'agent_close_own_poll':
            self.closed += 1
            self.polls[(args['chat_id'],args['message_id'])]['closed']=True
            if self.uncertain: raise TimeoutError()
            return {'status':'closed_verified','readback_verified':True}
        raise AssertionError('unexpected upstream operation')


class GatewayPollTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.registry=AgentRegistry(self.root/'agents.json')
        self.data={'schema_version':1,'agents':{'a':row({'read-polls','create-polls','close-own-polls'},saved=True),
                                                 'b':row({'read-polls'})}}
        self.registry.save(self.data); self.backend=FakeBackend()
        self.api=ReaderGateway(self.registry,self.backend,search_cursor_dir=self.root/'cursors')
        self.polls=PollGateway(self.api,self.root/'receipts')

    def tearDown(self): self.tmp.cleanup()

    async def test_saved_create_read_close_and_replay(self):
        r=await self.polls.create('a',1001,'Synthetic?', ['One','Two'],'poll-test-0001',True,False)
        self.assertEqual(r['status'],'delivered_verified'); self.assertTrue(r['readback_verified'])
        self.assertEqual(self.backend.sent,1)
        self.assertEqual((await self.polls.create('a',1001,'Synthetic?',['One','Two'],'poll-test-0001'))['message_id'],42)
        self.assertEqual(self.backend.sent,1)
        got=await self.polls.read('a',1001,42);self.assertEqual(got['total_voters'],0)
        closed=await self.polls.close('a',1001,42,'poll-close-001')
        self.assertEqual(closed['status'],'closed_verified'); self.assertEqual(self.backend.closed,1)
        await self.polls.close('a',1001,42,'poll-close-001');self.assertEqual(self.backend.closed,1)
        for p in (self.root/'receipts').rglob('*.json'):
            self.assertNotIn('Synthetic?',p.read_text())

    async def test_all_read_chats_is_not_poll_write_grant(self):
        with self.assertRaises(AccessDenied):
            await self.polls.create('a',-100123,'Q',['A','B'],'poll-denied-01')
        self.assertEqual(self.backend.sent,0)
        self.data['agents']['a']['poll_write_chat_ids']=[-100123];self.registry.save(self.data)
        r=await self.polls.create('a',-100123,'Q',['A','B'],'poll-allowed-1')
        self.assertEqual(r['status'],'delivered_verified')

    async def test_explicit_poll_all_grant_is_separate_and_revocable(self):
        self.data['agents']['a']['poll_write_saved']=False
        self.data['agents']['a']['poll_write_all_chats']=True;self.registry.save(self.data)
        targets=await self.polls.destinations('a')
        self.assertTrue(targets['all_readable_chats']);self.assertEqual(targets['saved_chat_id'],1001)
        for chat,request in ((1001,'poll-all-saved-1'),(-100123,'poll-all-group-1'),(7777,'poll-all-bot-01')):
            r=await self.polls.create('a',chat,'Q',['A','B'],request)
            self.assertEqual(r['status'],'delivered_verified')
        self.data['agents']['a']['poll_write_all_chats']=False;self.registry.save(self.data)
        with self.assertRaises(AccessDenied):
            await self.polls.create('a',-100123,'Q',['A','B'],'poll-all-revoked')

    async def test_poll_all_does_not_bypass_restricted_history_scope(self):
        self.data['agents']['a'].update(all_chats=False,chat_ids=[-100123],poll_write_all_chats=True)
        self.registry.save(self.data)
        with self.assertRaises(AccessDenied):
            await self.polls.create('a',-100999,'Q',['A','B'],'poll-unreadable')
        self.assertEqual(self.backend.sent,0)

    async def test_read_existing_without_any_write_grant(self):
        self.backend.polls[(-100123,55)]={'chat_id':-100123,'message_id':55,'poll_id':'6','question':'Existing',
            'options':[{'text':'A','votes':None},{'text':'B','votes':None}], 'total_voters':None, 'closed':False}
        result=await self.polls.read('b',-100123,55)
        self.assertEqual(result['message_id'],55);self.assertIsNone(result['total_voters'])
        with self.assertRaises(AccessDenied): await self.polls.create('b',-100123,'Q',['A','B'],'poll-denied-02')

    async def test_revocation_after_owner_lookup_blocks_dispatch(self):
        def revoke():
            self.data['agents']['a']['actions'].remove('create-polls');self.registry.save(self.data)
        self.backend.owner_hook=revoke
        with self.assertRaises(AccessDenied):
            await self.polls.create('a',1001,'Q',['A','B'],'poll-revoke-01')
        self.assertEqual(self.backend.sent,0)

    async def test_revoked_destination_blocks_receipt_replay(self):
        await self.polls.create('a',1001,'Q',['A','B'],'poll-target-01')
        self.data['agents']['a']['poll_write_saved']=False;self.registry.save(self.data)
        with self.assertRaises(AccessDenied):
            await self.polls.create('a',1001,'Q',['A','B'],'poll-target-01')
        self.assertEqual(self.backend.sent,1)

    async def test_uncertainty_blocks_same_and_new_id_for_identical_payload(self):
        self.backend.uncertain=True
        r=await self.polls.create('a',1001,'Q',['A','B'],'poll-uncertain-1')
        self.assertEqual(r['status'],'uncertain_do_not_retry')
        again=await self.polls.create('a',1001,'Q',['A','B'],'poll-uncertain-1')
        new=await self.polls.create('a',1001,'Q',['A','B'],'poll-uncertain-2')
        self.assertEqual(again['status'],'uncertain_do_not_retry')
        self.assertEqual(new['status'],'uncertain_do_not_retry');self.assertEqual(self.backend.sent,1)

    async def test_parallel_distinct_request_ids_cannot_duplicate_pending_poll(self):
        self.backend.pause=.05
        other=PollGateway(ReaderGateway(self.registry,self.backend,search_cursor_dir=self.root/'cursors2'),self.root/'receipts')
        a,b=await asyncio.gather(self.polls.create('a',1001,'Q',['A','B'],'poll-concurrent-1'),
                                 other.create('a',1001,'Q',['A','B'],'poll-concurrent-2'))
        self.assertEqual(self.backend.sent,1)
        self.assertEqual({a['status'],b['status']},{'delivered_verified','uncertain_do_not_retry'})

    async def test_close_foreign_poll_denied_before_write(self):
        self.backend.polls[(1001,51)]={'chat_id':1001,'message_id':51,'poll_id':'1','owner_created':False,'closed':False}
        result=await self.polls.close('a',1001,51,'poll-foreign-1')
        self.assertEqual(result['error'],'poll_not_verified_as_own');self.assertEqual(self.backend.closed,0)

    async def test_unknown_close_is_not_repeated_with_a_new_id(self):
        await self.polls.create('a',1001,'Q',['A','B'],'poll-before-close')
        self.backend.uncertain=True
        one=await self.polls.close('a',1001,42,'poll-close-unknown-1')
        two=await self.polls.close('a',1001,42,'poll-close-unknown-2')
        self.assertEqual(one['status'],'uncertain_do_not_retry')
        self.assertEqual(two['status'],'uncertain_do_not_retry');self.assertEqual(self.backend.closed,1)

    async def test_visible_wrong_quiz_answer_is_not_reported_verified(self):
        self.backend.reported_correct=0
        result=await self.polls.create('a',1001,'Q',['A','B'],'poll-quiz-wrong',True,False,1)
        self.assertEqual(result['status'],'readback_mismatch_do_not_retry')
        self.assertFalse(result['readback_verified'])
        retry=await self.polls.create('a',1001,'Q',['A','B'],'poll-quiz-retry',True,False,1)
        self.assertEqual(retry['status'],'uncertain_do_not_retry');self.assertEqual(self.backend.sent,1)


class BackendPollTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.home=Path(self.tmp.name);os.chmod(self.home,0o700)
        self.owner=types.SimpleNamespace(id=1001,bot=False)
        self.entity=tg.User(id=1001,first_name='Synthetic')
        self.messages={};self.sent=0;self.edits=0;self.uncertain=False;self.correct=None;self.result_updates=[]
        test=self
        class Client:
            async def get_me(_): return test.owner
            async def get_messages(_,entity,ids): return test.messages.get(ids)
            async def __call__(_,req):
                if isinstance(req,functions.messages.SendMediaRequest):
                    test.sent+=1;test.correct=req.media.correct_answers
                    bytes(req)  # Exercise the pinned Telethon serializer, including quiz indices.
                    sent=req.media.poll
                    assert sent.id == 0 and all(isinstance(x,tg.InputPollAnswer) for x in sent.answers)
                    poll=tg.Poll(id=987,hash=0,question=sent.question,
                        answers=[tg.PollAnswer(text=x.text,option=bytes([i])) for i,x in enumerate(sent.answers)],
                        public_voters=sent.public_voters,multiple_choice=sent.multiple_choice,quiz=sent.quiz,creator=True)
                    msg=types.SimpleNamespace(id=71,media=tg.MessageMediaPoll(poll=poll,results=tg.PollResults(total_voters=0)),out=True,sender_id=1001)
                    test.messages[71]=msg
                    if test.uncertain: raise TimeoutError()
                    return types.SimpleNamespace(updates=[tg.UpdateMessageID(id=71,random_id=req.random_id)])
                if isinstance(req,functions.messages.GetPollResultsRequest): return types.SimpleNamespace(updates=test.result_updates)
                if isinstance(req,functions.messages.EditMessageRequest):
                    bytes(req);test.edits+=1;test.messages[req.id].media.poll.closed=req.media.poll.closed;return object()
                raise AssertionError('unexpected request')
        self.client=Client();self.tools={}
        class MCP:
            def tool(_,**kw):
                def decorate(fn):test.tools[fn.__name__]=fn;return fn
                return decorate
        async def resolve(chat_id,client):return self.entity
        runtime=types.SimpleNamespace(mcp=MCP(),ToolAnnotations=ToolAnnotations,with_account=lambda **kw:lambda fn:fn,
            get_client=lambda account:self.client,resolve_entity=resolve,
            get_marked_id=lambda e:-100123 if isinstance(e,tg.Channel) else e.id,
            is_chat_allowlist_enabled=lambda:False)
        self.home_patch=patch('telegram_setup.poll_backend.private_dir',return_value=self.home);self.home_patch.start()
        register_poll_backend(runtime)

    def tearDown(self):self.home_patch.stop();self.tmp.cleanup()

    async def test_real_tl_poll_roundtrip_with_quiz_and_own_close(self):
        create=self.tools['agent_create_poll'];read=self.tools['agent_read_poll'];close=self.tools['agent_close_own_poll']
        result=await create(chat_id=1001,question='Fixture?',options=['A','B'],request_id='backend-poll-01',
                            agent_id='agent',anonymous=True,multiple_choice=False,quiz_correct_index=1)
        self.assertEqual(result['status'],'delivered_verified');self.assertEqual(self.correct,[1]);self.assertEqual(self.sent,1)
        poll=await read(chat_id=1001,message_id=71);self.assertEqual(poll['question'],'Fixture?')
        self.assertIsNone(poll['options'][0]['votes'])
        result=await close(chat_id=1001,message_id=71,request_id='backend-close-01',agent_id='agent')
        self.assertEqual(result['status'],'closed_verified');self.assertEqual(self.edits,1)

    async def test_unknown_dispatch_never_repeats_even_with_new_id(self):
        self.uncertain=True;fn=self.tools['agent_create_poll']
        args=dict(chat_id=1001,question='Q?',options=['A','B'],agent_id='agent')
        one=await fn(**args,request_id='backend-unknown-1')
        two=await fn(**args,request_id='backend-unknown-2')
        self.assertEqual(one['status'],'uncertain_do_not_retry');self.assertEqual(two['status'],'uncertain_do_not_retry')
        self.assertEqual(self.sent,1)
        three=await fn(**dict(args,agent_id='another-agent'),request_id='backend-unknown-3')
        self.assertEqual(three['status'],'uncertain_do_not_retry');self.assertEqual(self.sent,1)
        self.assertNotIn('original_request_id',three)

    async def test_foreign_poll_cannot_be_closed(self):
        poll=tg.Poll(id=1,hash=0,question=tg.TextWithEntities(text='Q',entities=[]),answers=[
            tg.PollAnswer(text=tg.TextWithEntities(text='A',entities=[]),option=b'0')])
        self.messages[71]=types.SimpleNamespace(id=71,media=tg.MessageMediaPoll(poll=poll,results=tg.PollResults()),out=False,sender_id=22)
        result=await self.tools['agent_close_own_poll'](chat_id=1001,message_id=71,request_id='backend-foreign-1',agent_id='agent')
        self.assertEqual(result['error'],'poll_not_owned_by_account');self.assertEqual(self.edits,0)

    async def test_public_voters_in_broadcast_rejected_before_send(self):
        self.entity=tg.Channel(id=123,title='Synthetic',photo=None,date=None,broadcast=True)
        result=await self.tools['agent_create_poll'](chat_id=-100123,question='Q',options=['A','B'],
            request_id='channel-public-1',agent_id='agent',anonymous=False)
        self.assertEqual(result,{'error':'public_votes_not_allowed_in_channel','sent':False})
        self.assertEqual(self.sent,0)

    async def test_regular_private_peer_rejected_but_bot_peer_is_supported(self):
        self.entity=tg.User(id=222,first_name='Synthetic')
        args=dict(chat_id=222,question='Q',options=['A','B'],agent_id='agent')
        result=await self.tools['agent_create_poll'](**args,request_id='private-person-1')
        self.assertEqual(result,{'error':'poll_private_peer_not_supported','sent':False});self.assertEqual(self.sent,0)
        self.entity=tg.User(id=222,first_name='Synthetic bot',bot=True,bot_info_version=1)
        result=await self.tools['agent_create_poll'](**args,request_id='private-bot-0001')
        self.assertEqual(result['status'],'delivered_verified');self.assertEqual(self.sent,1)

    async def test_result_refresh_reports_aggregate_without_voter_ids(self):
        await self.tools['agent_create_poll'](chat_id=1001,question='Q',options=['A','B'],
            request_id='backend-results-1',agent_id='agent')
        result=tg.PollResults(total_voters=2,results=[tg.PollAnswerVoters(option=b'\x00',voters=2)],
                              recent_voters=[tg.PeerUser(user_id=55)])
        self.result_updates=[tg.UpdateMessagePoll(poll_id=987,results=result)]
        snap=await self.tools['agent_read_poll'](chat_id=1001,message_id=71)
        self.assertEqual(snap['total_voters'],2);self.assertEqual(snap['options'][0]['votes'],2)
        self.assertIsNone(snap['options'][1]['votes']);self.assertNotIn('recent_voters',str(snap))


class PollMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.registry=AgentRegistry(self.root/'agents.json')
        self.data={'schema_version':1,'agents':{'a':row({'read-polls','create-polls','close-own-polls'},saved=True)}}
        self.registry.save(self.data)
    def tearDown(self):self.tmp.cleanup()

    async def test_default_facade_has_no_poll_tools(self):
        @contextlib.asynccontextmanager
        async def upstream(): yield FakeBackend()
        mcp=create_gateway(self.registry,'a',upstream)
        names={x.name for x in await mcp.list_tools()}
        self.assertFalse(names & {'create_poll','get_poll','close_own_poll','list_poll_destinations'})
        with self.assertRaises(ValueError): create_gateway(self.registry,None,upstream,enable_polls=True)

    async def test_real_stdio_schema_scope_readback_and_spoof_denial(self):
        script=Path(__file__).with_name('offline_polls.py')
        params=StdioServerParameters(command=sys.executable,args=[str(script),str(self.registry.path),str(self.root/'receipts')])
        with open(os.devnull,'w') as err:
            async with stdio_client(params,errlog=err) as (reader,writer):
                async with ClientSession(reader,writer) as session:
                    await session.initialize();catalog=(await session.list_tools()).tools
                    tools={t.name:t for t in catalog}
                    self.assertTrue({'get_poll','create_poll','close_own_poll','list_poll_destinations'} <= tools.keys())
                    self.assertFalse(tools['create_poll'].annotations.readOnlyHint)
                    self.assertTrue(tools['get_poll'].annotations.readOnlyHint)
                    target=text_payload(await session.call_tool('list_poll_destinations',{}))
                    self.assertEqual(target['saved_chat_id'],1001)
                    args={'chat_id':1001,'question':'Synthetic?','options':['Yes','No'],'request_id':'poll-stdio-001'}
                    result=await session.call_tool('create_poll',args)
                    self.assertFalse(result.isError);self.assertEqual(text_payload(result)['status'],'delivered_verified')
                    result=await session.call_tool('create_poll',dict(args,agent_id='other'))
                    self.assertTrue(result.isError)
                    result=await session.call_tool('create_poll',dict(args,request_id='poll-stdio-002',chat_id=-100999))
                    self.assertTrue(result.isError)
                    result=await session.call_tool('get_poll',{'chat_id':1001,'message_id':42})
                    self.assertFalse(result.isError);self.assertEqual(text_payload(result)['message_id'],42)
                    result=await session.call_tool('close_own_poll',{'chat_id':1001,'message_id':42,'request_id':'poll-close-std1'})
                    self.assertFalse(result.isError);self.assertEqual(text_payload(result)['status'],'closed_verified')

    async def test_status_reports_saved_in_explicit_all_chat_scope(self):
        self.data['agents']['a'].update(poll_write_saved=False,poll_write_all_chats=True)
        self.registry.save(self.data)
        script=Path(__file__).with_name('offline_polls.py')
        params=StdioServerParameters(command=sys.executable,args=[str(script),str(self.registry.path),str(self.root/'receipts')])
        with open(os.devnull,'w') as err:
            async with stdio_client(params,errlog=err) as (reader,writer):
                async with ClientSession(reader,writer) as session:
                    await session.initialize();status=text_payload(await session.call_tool('telegram_status',{}))
                    self.assertTrue(status['poll_write_all_chats']);self.assertTrue(status['poll_saved_target'])
                    self.assertFalse(status['poll_saved_explicit'])

    async def test_real_stdio_backend_capabilities_are_separate_and_default_off(self):
        script=Path(__file__).with_name('offline_server.py')
        root=Path(__file__).resolve().parents[1]
        if not (root/'.upstream'/'main.py').is_file(): self.skipTest('pinned upstream not staged')
        for selected in (['read'],['read','agent-poll-read'],
                         ['read','agent-poll-read','agent-poll-create','agent-poll-close']):
            with tempfile.TemporaryDirectory() as home:
                with patch.dict(os.environ,{'AGENT_TELEGRAM_HOME':home}):
                    save_settings({'schema_version':1,'enabled':selected,'transcription_engine':None})
                params=StdioServerParameters(command=sys.executable,args=[str(script)],cwd=str(root),
                                             env={'AGENT_TELEGRAM_HOME':home})
                with open(os.devnull,'w') as err:
                    async with stdio_client(params,errlog=err) as (reader,writer):
                        async with ClientSession(reader,writer) as session:
                            await session.initialize();names={t.name for t in (await session.list_tools()).tools}
                            self.assertEqual('agent_read_poll' in names,'agent-poll-read' in selected)
                            self.assertEqual('agent_create_poll' in names,'agent-poll-create' in selected)
                            self.assertEqual('agent_close_own_poll' in names,'agent-poll-close' in selected)
                            self.assertFalse({'create_poll','vote_poll'} & names)


class PollEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_through_real_backend_adapter_to_fake_telegram(self):
        fixture=BackendPollTests(methodName='test_foreign_poll_cannot_be_closed');fixture.setUp()
        try:
            registry=AgentRegistry(fixture.home/'agents.json')
            registry.save({'schema_version':1,'agents':{'agent':row({'read-polls','create-polls','close-own-polls'},saved=True)}})
            class Bridge:
                available_tools={'agent_read_poll','agent_create_poll','agent_close_own_poll'}
                async def call(_,name,args):
                    if name=='get_me': return {'type':'user','id':1001}
                    return await fixture.tools[name](**args)
            api=ReaderGateway(registry,Bridge(),search_cursor_dir=fixture.home/'cursors')
            polls=PollGateway(api,fixture.home/'gateway-receipts')
            result=await polls.create('agent',1001,'Fixture?',['A','B'],'endtoend-poll-1')
            self.assertEqual(result['status'],'delivered_verified');self.assertEqual(fixture.sent,1)
            read=await polls.read('agent',1001,result['message_id']);self.assertEqual(read['question'],'Fixture?')
            closed=await polls.close('agent',1001,result['message_id'],'endtoend-close-1')
            self.assertEqual(closed['status'],'closed_verified');self.assertEqual(fixture.edits,1)
            again=await polls.create('agent',1001,'Fixture?',['A','B'],'endtoend-poll-1')
            self.assertEqual(again['status'],'delivered_verified');self.assertEqual(fixture.sent,1)
        finally:fixture.tearDown()


if __name__=='__main__':unittest.main()
