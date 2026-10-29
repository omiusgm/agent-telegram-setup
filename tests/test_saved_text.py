"""No real Telegram. Opt-in Saved Messages send, readback, denial and replay."""
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import Tool, ToolAnnotations

from telegram_setup.agents import AccessDenied, SAVED_TEXT_ACTION
from telegram_setup.client import text_payload
from telegram_setup.config import ROOT
from telegram_setup.gateway import create_gateway, validate_backend_catalog
from telegram_setup.saved_text import SavedTextGateway
from test_agent_gateway import FakeUpstream, RegistryFixture


class FakeSavedUpstream(FakeUpstream):
    send_text_available = True
    fail_send = False
    bad_readback = False
    readback_flood = False

    def __init__(self):
        super().__init__()
        self.sent = []

    async def call(self, name, args):
        if name == 'send_message':
            self.calls.append((name, args))
            assert set(args) == {'chat_id', 'message', 'parse_mode'}
            assert args['chat_id'] == 'me' and args['parse_mode'] is None
            self.sent.append(args['message'])
            if self.fail_send:
                raise TimeoutError('synthetic response lost after send')
            return 'Message sent successfully. message_id=501'
        if name == 'read_history_page' and args.get('until_id') == 501:
            self.calls.append((name, args))
            if self.readback_flood:
                return {'error': 'synthetic_flood_wait', 'retry_after_seconds': 30}
            assert args == {'chat_id': 123456, 'after_id': 500, 'until_id': 501, 'limit': 1}
            return {'chat_id': 123456, 'messages': [{'id': 501,
                    'text': 'synthetic-mismatch' if self.bad_readback else self.sent[-1]}]}
        return await super().call(name, args)


class SavedTextTests(RegistryFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_registry()
        self.upstream = FakeSavedUpstream()
        self.folder = Path(self.temporary.name) / 'receipts'
        self.api = SavedTextGateway(self.registry, self.upstream, self.folder)

    def tearDown(self):
        self.teardown_registry()

    def grant(self):
        self.data['agents']['a']['actions'].append(SAVED_TEXT_ACTION)
        self.registry.save(self.data)

    async def test_default_reader_denies_before_any_upstream_or_receipt(self):
        with self.assertRaises(AccessDenied):
            await self.api.send_saved_text('a', 'synthetic text', 'request-001')
        self.assertEqual(self.upstream.calls, [])
        self.assertFalse(self.folder.exists())

    async def test_saved_target_plain_text_exact_readback_and_replay_after_restart(self):
        self.grant()
        text = 'Synthetic **plain** text and emoji 🙂'
        first = await self.api.send_saved_text('a', text, 'request-001')
        self.assertEqual(first['status'], 'delivered_verified')
        self.assertTrue(first['readback_verified'])
        self.assertEqual(first['message_id'], 501)
        self.assertEqual(first['destination'], 'saved_messages')
        before = len(self.upstream.calls)
        self.upstream.send_text_available = False
        restarted = SavedTextGateway(self.registry, self.upstream, self.folder)
        self.assertEqual(await restarted.send_saved_text('a', text, 'request-001'), first)
        self.assertEqual(len(self.upstream.calls), before)
        self.upstream.send_text_available = True
        self.assertEqual(self.upstream.sent, [text])
        receipt = next(self.folder.glob('*.json'))
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(text, receipt.read_text())
        with self.assertRaises(ValueError):
            await restarted.send_saved_text('a', 'changed text', 'request-001')
        self.assertEqual(len(self.upstream.sent), 1)

    async def test_lost_reply_and_bad_readback_never_allow_blind_resend(self):
        self.grant()
        for index, kind in enumerate(('timeout', 'readback')):
            self.upstream.fail_send = kind == 'timeout'
            self.upstream.bad_readback = kind == 'readback'
            request = 'request-uncertain-' + str(index)
            result = await self.api.send_saved_text('a', 'synthetic text', request)
            self.assertEqual(result['status'], 'uncertain_do_not_retry')
            self.assertFalse(result['readback_verified'])
            count = len(self.upstream.sent)
            self.assertEqual(await self.api.send_saved_text('a', 'synthetic text', request), result)
            self.assertEqual(len(self.upstream.sent), count)

    async def test_revocation_of_queued_send_prevents_all_upstream_calls(self):
        self.grant()
        await self.api.lock.acquire()
        pending = asyncio.create_task(self.api.send_saved_text('a', 'synthetic', 'request-001'))
        await asyncio.sleep(0)
        self.data['agents']['a']['actions'].remove(SAVED_TEXT_ACTION)
        self.registry.save(self.data)
        self.api.lock.release()
        with self.assertRaises(AccessDenied):
            await pending
        self.assertEqual(self.upstream.calls, [])

    async def test_invalid_text_backend_disabled_and_cooldown_do_not_dispatch(self):
        self.grant()
        for text, request in (('', 'request-001'), ('🙂' * 2049, 'request-002'), ('ok', '../bad')):
            with self.assertRaises(ValueError):
                await self.api.send_saved_text('a', text, request)
        self.upstream.send_text_available = False
        self.assertFalse((await self.api.send_saved_text('a', 'ok', 'request-001'))['sent'])
        self.upstream.send_text_available = True
        self.api.retry_at = time.monotonic() + 30
        self.assertEqual((await self.api.send_saved_text('a', 'ok', 'request-001'))['error'], 'telegram_cooldown')
        self.assertEqual(self.upstream.calls, [])

    async def test_write_permission_is_independent_of_other_agent_and_read_access(self):
        self.grant()
        self.assertFalse((await self.api.status('a'))['read_only'])
        self.assertTrue((await self.api.status('b'))['read_only'])
        with self.assertRaises(AccessDenied):
            await self.api.send_saved_text('b', 'synthetic', 'request-001')
        self.data['agents']['a']['actions'].remove(SAVED_TEXT_ACTION)
        self.registry.save(self.data)
        self.assertTrue((await self.api.status('a'))['connected'])
        with self.assertRaises(AccessDenied):
            await self.api.send_saved_text('a', 'synthetic', 'request-001')

    async def test_readback_flood_wait_blocks_the_next_dispatch(self):
        self.grant()
        self.upstream.readback_flood = True
        first = await self.api.send_saved_text('a', 'synthetic', 'request-flood-001')
        self.assertEqual(first['status'], 'uncertain_do_not_retry')
        self.assertEqual(first['retry_after_seconds'], 30)
        before = len(self.upstream.calls)
        second = await self.api.send_saved_text('a', 'synthetic', 'request-flood-002')
        self.assertEqual(second['error'], 'telegram_cooldown')
        self.assertEqual(len(self.upstream.calls), before)

    async def test_claim_survives_crash_without_a_second_dispatch(self):
        self.grant()
        self.api.receipts.begin('a', 'request-crash-001', hashlib.sha256(b'synthetic').hexdigest())
        restarted = SavedTextGateway(self.registry, self.upstream, self.folder)
        result = await restarted.send_saved_text('a', 'synthetic', 'request-crash-001')
        self.assertEqual(result['status'], 'uncertain_do_not_retry')
        self.assertEqual(self.upstream.calls, [])

    async def test_two_gateway_process_states_share_the_same_durable_request_claim(self):
        self.grant()
        second = SavedTextGateway(self.registry, self.upstream, self.folder)
        receipts = await asyncio.gather(self.api.send_saved_text('a', 'synthetic', 'request-concurrent'),
                                        second.send_saved_text('a', 'synthetic', 'request-concurrent'))
        self.assertTrue(all(r['status'] in ('delivered_verified', 'uncertain_do_not_retry') for r in receipts))
        self.assertEqual(len(self.upstream.sent), 1)

    async def test_default_catalog_stays_read_only_even_if_profile_can_send(self):
        self.grant()
        @contextlib.asynccontextmanager
        async def upstream():
            yield self.upstream
        catalog = await create_gateway(self.registry, 'a', upstream).list_tools()
        self.assertEqual(len(catalog), 6)
        self.assertTrue(all(tool.annotations.readOnlyHint for tool in catalog))

    def test_cli_default_grant_and_revoke_are_separate_from_reads(self):
        command = [sys.executable, '-m', 'telegram_setup.agents', '--registry', str(self.registry.path)]
        token = Path(self.temporary.name) / 'new-reader.token'
        def run(*args):
            return subprocess.run(command + list(args), cwd=ROOT, capture_output=True, text=True, check=True)
        result = run('add', 'new-reader', '--chat-id', '11', '--token-file', str(token))
        self.assertTrue(json.loads(result.stdout)['read_only'])
        self.assertNotIn(token.read_text().strip(), result.stdout + result.stderr)
        self.assertNotIn(SAVED_TEXT_ACTION, self.registry.profile('new-reader')['actions'])
        run('grant-saved-text', 'new-reader')
        self.assertIn(SAVED_TEXT_ACTION, self.registry.profile('new-reader')['actions'])
        run('revoke-saved-text', 'new-reader')
        profile = self.registry.profile('new-reader')
        self.assertNotIn(SAVED_TEXT_ACTION, profile['actions'])
        self.assertIn('history', profile['actions'])
        self.assertEqual(profile['chat_ids'], [11])

    def test_backend_catalog_accepts_only_known_text_writes(self):
        def tool(name, readonly):
            return Tool(name=name, inputSchema={'type': 'object'},
                        annotations=ToolAnnotations(readOnlyHint=readonly))
        validate_backend_catalog([tool('get_me', True), tool('send_message', False), tool('reply_to_message', False),
                                  tool('join_owner_requested_source', False),
                                  tool('agent_create_poll', False), tool('agent_close_own_poll', False)])
        for bad in ('send_file', 'delete_message', 'mark_as_read', 'create_poll', 'vote_poll'):
            with self.assertRaises(RuntimeError):
                validate_backend_catalog([tool(bad, False)])

    async def test_real_stdio_mvp_catalog_permission_target_spoof_and_receipt(self):
        self.grant()
        parameters = StdioServerParameters(command=sys.executable,
            args=[str(ROOT / 'tests/offline_saved_text.py'), str(self.registry.path), str(self.folder)])
        with open(os.devnull, 'w') as log:
            async with stdio_client(parameters, errlog=log) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    catalog = (await session.list_tools()).tools
                    self.assertEqual(len(catalog), 7)
                    tool = next(t for t in catalog if t.name == 'send_saved_text')
                    self.assertFalse(tool.annotations.readOnlyHint)
                    self.assertTrue(tool.annotations.idempotentHint)
                    args = {'text': 'Synthetic stdio message', 'request_id': 'request-stdio-001'}
                    self.assertTrue((await session.call_tool('send_saved_text', dict(args, chat_id=22))).isError)
                    result = text_payload(await session.call_tool('send_saved_text', args))
                    self.assertEqual(result['status'], 'delivered_verified')
                    self.assertEqual(text_payload(await session.call_tool('send_saved_text', args)), result)
                    self.data['agents']['a']['actions'].remove(SAVED_TEXT_ACTION)
                    self.registry.save(self.data)
                    self.assertTrue((await session.call_tool('send_saved_text', dict(args, request_id='request-stdio-002'))).isError)
                    self.assertTrue(text_payload(await session.call_tool('telegram_status', {}))['connected'])


if __name__ == '__main__':
    unittest.main()
