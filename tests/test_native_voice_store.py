"""Synthetic durable voice claims: no Telegram, audio, or external service."""
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from telegram_setup.native_voice_store import NativeVoiceStore


class NativeVoiceStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name) / 'voice-cache'
        self.store = NativeVoiceStore(self.root)
    def tearDown(self): self.tmp.cleanup()

    def test_claim_before_dispatch_replay_and_cross_agent_cache(self):
        first, dispatch = self.store.claim(101, 'a', 11, 5, 'voice-test-001')
        self.assertTrue(dispatch); self.assertEqual(first['status'], 'uncertain_do_not_retry')
        replay, dispatch = self.store.claim(101, 'a', 11, 5, 'voice-test-001')
        self.assertFalse(dispatch); self.assertEqual(replay, first)
        other, dispatch = self.store.claim(101, 'b', 11, 5, 'voice-test-002')
        self.assertFalse(dispatch); self.assertEqual(other, first)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        for path in self.root.rglob('*.json'): self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_request_id_collision_and_missing_voice_fail_closed(self):
        self.store.claim(101, 'a', 11, 5, 'voice-test-001')
        with self.assertRaises(ValueError): self.store.lookup(101, 'a', 11, 6, 'voice-test-001')
        for file in (self.root / 'voices').glob('*.json'): file.unlink()
        with self.assertRaises(ValueError): self.store.lookup(101, 'a', 11, 5, 'voice-test-001')
        with self.assertRaises(ValueError): self.store.claim(101, 'a', 11, 5, 'voice-test-001')

    def test_only_same_request_can_retry_after_server_flood_wait(self):
        with patch('telegram_setup.native_voice_store.time.time', return_value=1000):
            self.store.claim(101, 'a', 11, 5, 'voice-test-001')
            self.store.update(101, 11, 5, {'status': 'flood_wait', 'retry_at': 1010})
        with patch('telegram_setup.native_voice_store.time.time', return_value=1009):
            self.assertFalse(self.store.lookup(101, 'a', 11, 5, 'voice-test-001')[1])
        with patch('telegram_setup.native_voice_store.time.time', return_value=1010):
            self.assertFalse(self.store.lookup(101, 'b', 11, 5, 'voice-test-002')[1])
            self.assertTrue(self.store.lookup(101, 'a', 11, 5, 'voice-test-001')[1])
            record, dispatch = self.store.claim(101, 'a', 11, 5, 'voice-test-001')
            self.assertTrue(dispatch); self.assertEqual(record['status'], 'uncertain_do_not_retry')
            self.assertFalse(self.store.claim(101, 'a', 11, 5, 'voice-test-001')[1])

    def test_matching_update_completes_and_stale_updates_do_not_overwrite(self):
        self.store.claim(101, 'a', 11, 5, 'voice-test-001')
        self.store.update(101, 11, 5, {'status': 'pending', 'transcription_id': 99, 'text': 'partial'})
        self.assertFalse(self.store.apply_update(101, 11, 5, 100, 'wrong', False))
        self.assertTrue(self.store.apply_update(101, 11, 5, 99, 'complete', False))
        self.assertFalse(self.store.apply_update(101, 11, 5, 99, 'late partial', True))
        self.assertEqual(self.store.get(101, 11, 5)['text'], 'complete')

    def test_unsafe_directory_and_symlink_record_fail_closed(self):
        self.root.symlink_to(Path(self.tmp.name), target_is_directory=True)
        with self.assertRaises(PermissionError): self.store.claim(101, 'a', 11, 5, 'voice-test-001')
        self.root.unlink(); self.store.claim(101, 'a', 11, 5, 'voice-test-001')
        voice = next((self.root / 'voices').glob('*.json')); voice.unlink()
        target = Path(self.tmp.name) / 'other'; target.write_text('{}'); voice.symlink_to(target)
        with self.assertRaises(OSError): self.store.get(101, 11, 5)


if __name__ == '__main__': unittest.main()
