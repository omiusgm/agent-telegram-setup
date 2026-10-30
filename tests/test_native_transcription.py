"""Telegram-native voice transcription using fake TL objects, never a real account."""
import asyncio
from datetime import datetime, timezone
import json
import time
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from telethon import errors
from telethon.tl import functions, types

from telegram_setup.agents import AccessDenied
from telegram_setup.native_transcription import NativeVoiceService
from telegram_setup.native_transcription_gateway import NativeTranscriptionGateway
from telegram_setup.native_voice_store import NativeVoiceStore
from telegram_setup.reader_gateway import ReaderGateway
from telegram_setup.sensitive_peer import BOTFATHER_ID
from test_agent_gateway import RegistryFixture
from test_sensitive_peer import TOKEN


def document(*, voice=True, round_video=False):
    attrs = [types.DocumentAttributeVideo(duration=3, w=1, h=1, round_message=True)] if round_video else [
        types.DocumentAttributeAudio(duration=3, voice=voice)]
    return types.Document(id=1, access_hash=2, file_reference=b'fake', date=datetime.now(timezone.utc),
                          mime_type='audio/ogg', size=5, dc_id=1, attributes=attrs)


class FakeClient:
    def __init__(self):
        self.calls = []; self.fetches = []; self.handler = None; self.early = None; self.early_task = None
        self.error = None; self.after_rpc = None; self.message_voice = True; self.round_video = False
        self.reply = types.messages.TranscribedAudio(transcription_id=77, text='synthetic final')
    def add_event_handler(self, handler, event): self.handler = handler
    async def get_messages(self, entity, ids):
        self.fetches.append((entity.id, ids))
        return NS(id=ids, chat_id=entity.id, media=types.MessageMediaDocument(
            document=document(voice=self.message_voice, round_video=self.round_video)))
    async def get_input_entity(self, entity): return types.InputPeerUser(user_id=entity.id, access_hash=1)
    async def __call__(self, request, **kwargs):
        assert isinstance(request, functions.messages.TranscribeAudioRequest)
        assert kwargs == {'flood_sleep_threshold': 0}
        self.calls.append(request)
        if self.early is not None:
            self.early_task = asyncio.create_task(self.handler(self.early)); await asyncio.sleep(0)
        if self.error is not None:
            exc, self.error = self.error, None
            raise exc
        if self.after_rpc: self.after_rpc()
        return self.reply
    async def download_media(self, *args, **kwargs): raise AssertionError('audio download is forbidden')


class NativeTranscriptionTests(RegistryFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup_registry(); self.client = FakeClient()
        async def resolve(chat_id, _client): return NS(id=chat_id)
        self.runtime = NS(resolve_entity=resolve, get_marked_id=lambda entity: entity.id,
                          is_chat_allowlist_enabled=lambda: False)
        self.store = NativeVoiceStore(Path(self.temporary.name) / 'voice-cache')
        self.service = NativeVoiceService(self.runtime, self.client, self.registry, 101, self.store)
        self.service.attach(); await self.service.activate(101)
    async def asyncTearDown(self): self.teardown_registry()

    async def test_final_transcript_is_cached_without_redownload_or_repeat(self):
        self.client.reply = types.messages.TranscribedAudio(transcription_id=77, text='synthetic final',
            trial_remains_num=1, trial_remains_until_date=datetime(2026, 11, 1, tzinfo=timezone.utc))
        result = await self.service.start('a', 11, 5, 'voice-test-001')
        replay = await self.service.start('a', 11, 5, 'voice-test-002')
        readback = await self.service.get('a', 11, 5)
        self.assertEqual(result['status'], 'complete'); self.assertEqual(result['text'], 'synthetic final')
        self.assertEqual(result['trial_remains_num'], 1); self.assertIn('2026-11-01', result['trial_reset_utc'])
        self.assertTrue(replay['cached']); self.assertEqual(readback['text'], result['text'])
        self.assertEqual(len(self.client.calls), 1); self.assertEqual(len(self.client.fetches), 1)

    async def test_concurrent_requests_for_same_voice_send_only_one_rpc(self):
        first, second = await asyncio.gather(self.service.start('a', 11, 5, 'voice-test-001'),
                                             self.service.start('a', 11, 5, 'voice-test-002'))
        self.assertEqual((first['status'], second['status']), ('complete', 'complete'))
        self.assertEqual(len(self.client.calls), 1)

    async def test_pending_update_before_rpc_reply_is_not_lost(self):
        self.client.reply = types.messages.TranscribedAudio(transcription_id=77, text='partial', pending=True)
        self.client.early = types.UpdateTranscribedAudio(peer=types.PeerUser(11), msg_id=5,
                                                        transcription_id=77, text='synthetic complete')
        initial = await self.service.start('a', 11, 5, 'voice-test-001')
        self.assertEqual(initial['status'], 'pending'); self.assertTrue(self.service.has_pending())
        await self.client.early_task
        final = await self.service.get('a', 11, 5)
        self.assertEqual((final['status'], final['text']), ('complete', 'synthetic complete'))
        self.assertFalse(self.service.has_pending())
        self.assertEqual(len(self.client.calls), 1)

    async def test_wrong_update_is_ignored_and_restart_can_capture_pending_update(self):
        self.client.reply = types.messages.TranscribedAudio(transcription_id=77, text='partial', pending=True)
        await self.service.start('a', 11, 5, 'voice-test-001')
        for peer, msg, ident in ((11, 5, 999), (11, 6, 77), (22, 5, 77)):
            await self.service.on_update(types.UpdateTranscribedAudio(peer=types.PeerUser(peer), msg_id=msg,
                                                                     transcription_id=ident, text='wrong'))
        self.assertEqual((await self.service.get('a', 11, 5))['status'], 'pending')
        new_client = FakeClient()
        resumed = NativeVoiceService(self.runtime, new_client, self.registry, 101, self.store)
        resumed.attach()
        await resumed.on_update(types.UpdateTranscribedAudio(peer=types.PeerUser(11), msg_id=5,
                                                             transcription_id=77, text='after restart'))
        self.assertEqual((await self.service.get('a', 11, 5))['status'], 'pending')
        await resumed.activate(101)
        self.assertEqual((await resumed.get('a', 11, 5))['text'], 'after restart')
        self.assertEqual(new_client.calls, [])

    async def test_pending_keepalive_is_bounded_without_repeating_the_rpc(self):
        self.client.reply = types.messages.TranscribedAudio(transcription_id=77, text='partial', pending=True)
        await self.service.start('a', 11, 5, 'voice-test-001')
        self.assertTrue(self.service.has_pending())
        self.service.pending_deadlines[(11, 5)] = time.monotonic() - 1
        self.assertFalse(self.service.has_pending())
        self.assertEqual((await self.service.get('a', 11, 5))['status'], 'pending')
        self.assertEqual(len(self.client.calls), 1)

    async def test_unverified_account_cannot_persist_or_return_transcript(self):
        self.service.verified = False
        await self.service.on_update(types.UpdateTranscribedAudio(peer=types.PeerUser(11), msg_id=5,
                                                                  transcription_id=77, text='untrusted fixture'))
        with self.assertRaises(PermissionError): await self.service.get('a', 11, 5)
        with self.assertRaises(PermissionError): await self.service.activate(999)
        self.assertEqual(self.service.early_updates, []); self.assertFalse(self.store.root.exists())

    async def test_unsafe_optional_cache_disables_only_voice_feature(self):
        self.store.root.symlink_to(Path(self.temporary.name), target_is_directory=True)
        other = NativeVoiceService(self.runtime, FakeClient(), self.registry, 101, self.store)
        other.attach(); await other.activate(101)
        self.assertTrue(other.verified); self.assertFalse(other.cache_ready)
        with self.assertRaises(PermissionError): await other.start('a', 11, 5, 'voice-test-001')
        self.assertEqual(other.client.calls, []); self.assertTrue(self.registry.profile('a')['enabled'])

    async def test_flood_wait_is_honoured_and_only_same_request_retries_afterward(self):
        self.client.error = errors.FloodWaitError(request=None, capture=10)
        with patch('time.time', return_value=1000):
            result = await self.service.start('a', 11, 5, 'voice-test-001')
            self.assertEqual(result['status'], 'flood_wait'); self.assertEqual(result['retry_after_seconds'], 10)
        with patch('time.time', return_value=1005):
            still = await self.service.start('a', 11, 5, 'voice-test-001')
            self.assertEqual(still['retry_after_seconds'], 5); self.assertEqual(len(self.client.calls), 1)
        with patch('time.time', return_value=1010):
            other = await self.service.start('a', 11, 5, 'voice-test-002')
            self.assertEqual(other['status'], 'flood_wait'); self.assertEqual(len(self.client.calls), 1)
            done = await self.service.start('a', 11, 5, 'voice-test-001')
            self.assertEqual(done['status'], 'complete'); self.assertEqual(len(self.client.calls), 2)

    async def test_premium_or_quota_rejection_never_buys_or_retries(self):
        self.client.error = errors.PremiumAccountRequiredError(request=None)
        result = await self.service.start('a', 11, 5, 'voice-test-001')
        self.assertEqual((result['status'], result['reason']), ('rejected', 'premium_or_quota_required'))
        again = await self.service.start('a', 11, 5, 'voice-test-002')
        self.assertTrue(again['cached']); self.assertEqual(len(self.client.calls), 1)

    async def test_uncertain_timeout_never_repeats_with_another_request(self):
        self.client.error = TimeoutError('sensitive provider text')
        result = await self.service.start('a', 11, 5, 'voice-test-001')
        again = await self.service.start('a', 11, 5, 'voice-test-002')
        self.assertEqual(result['status'], 'uncertain_do_not_retry')
        self.assertEqual(again['status'], 'uncertain_do_not_retry'); self.assertEqual(len(self.client.calls), 1)
        self.assertNotIn('sensitive provider text', json.dumps(result))

    async def test_cancellation_after_dispatch_preserves_claim_and_never_repeats(self):
        sent = asyncio.Event()
        class HangingClient(FakeClient):
            async def __call__(inner, request, **kwargs):
                inner.calls.append(request); sent.set(); await asyncio.Event().wait()
        client = HangingClient()
        service = NativeVoiceService(self.runtime, client, self.registry, 101, self.store)
        service.attach(); await service.activate(101)
        task = asyncio.create_task(service.start('a', 11, 5, 'voice-test-001'))
        await sent.wait(); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        again = await service.start('a', 11, 5, 'voice-test-002')
        self.assertEqual(again['status'], 'uncertain_do_not_retry'); self.assertEqual(len(client.calls), 1)

    async def test_wrong_scope_botfather_and_music_are_denied_before_transcription(self):
        with self.assertRaises(AccessDenied): await self.service.start('a', 22, 5, 'voice-test-001')
        self.data['agents']['a']['all_chats'] = True; self.registry.save(self.data)
        with self.assertRaises(PermissionError): await self.service.start('a', BOTFATHER_ID, 5, 'voice-test-002')
        self.client.message_voice = False
        result = await self.service.start('a', 11, 5, 'voice-test-003')
        self.assertEqual(result['reason'], 'not_voice'); self.assertEqual(self.client.calls, [])

    async def test_round_video_is_submitted_to_telegram_without_download(self):
        self.client.round_video = True
        result = await self.service.start('a', 11, 5, 'voice-test-001')
        self.assertEqual(result['status'], 'complete'); self.assertEqual(len(self.client.calls), 1)

    async def test_signed_tl_transcription_id_is_accepted_and_correlated(self):
        self.client.reply = types.messages.TranscribedAudio(transcription_id=-77, text='partial', pending=True)
        result = await self.service.start('a', 11, 5, 'voice-test-001')
        self.assertEqual(result['transcription_id'], -77)
        await self.service.on_update(types.UpdateTranscribedAudio(peer=types.PeerUser(11), msg_id=5,
                                                                  transcription_id=-77, text='complete'))
        self.assertEqual((await self.service.get('a', 11, 5))['status'], 'complete')

    async def test_out_of_range_message_id_is_rejected_before_any_rpc(self):
        with self.assertRaises(ValueError): await self.service.start('a', 11, 2**31, 'voice-test-001')
        self.assertEqual(self.client.calls, []); self.assertEqual(self.client.fetches, [])

    async def test_revoked_read_scope_never_returns_completed_text(self):
        def revoke():
            self.data['agents']['a']['actions'].remove('history'); self.registry.save(self.data)
        self.client.after_rpc = revoke
        with self.assertRaises(AccessDenied): await self.service.start('a', 11, 5, 'voice-test-001')
        with self.assertRaises(AccessDenied): await self.service.get('a', 11, 5)

    async def test_large_result_is_not_silently_truncated_or_retried(self):
        self.client.reply = types.messages.TranscribedAudio(transcription_id=77, text='x' * 262145)
        result = await self.service.start('a', 11, 5, 'voice-test-001')
        self.assertEqual(result['status'], 'result_too_large'); self.assertNotIn('text', result)
        again = await self.service.start('a', 11, 5, 'voice-test-002')
        self.assertEqual(again['status'], 'result_too_large'); self.assertEqual(len(self.client.calls), 1)

    async def test_large_matching_update_is_reported_instead_of_silent_pending(self):
        self.client.reply = types.messages.TranscribedAudio(transcription_id=77, text='partial', pending=True)
        await self.service.start('a', 11, 5, 'voice-test-001')
        await self.service.on_update(types.UpdateTranscribedAudio(peer=types.PeerUser(11), msg_id=5,
                                                                  transcription_id=77, text='x' * 262145))
        result = await self.service.get('a', 11, 5)
        self.assertEqual(result['status'], 'result_too_large'); self.assertNotIn('text', result)

    async def test_cloud_facade_marks_recognisable_secret_redaction(self):
        class Bridge:
            available_tools = {'agent_get_telegram_voice_transcript'}
            async def call(_, name, args):
                return {'chat_id':11, 'message_id':5, 'status':'complete', 'source':'telegram_native',
                        'transcription_id':-7, 'text':'synthetic ' + TOKEN, 'complete':True}
        api = ReaderGateway(self.registry, Bridge())
        result = await NativeTranscriptionGateway(api).call('a', 11, 5)
        self.assertTrue(result['sensitive_text_redacted']); self.assertNotIn(TOKEN, result['text'])
