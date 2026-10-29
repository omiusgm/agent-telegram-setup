"""Synthetic filesystem fault tests; no Telegram session or real credentials."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from telegram_setup.newbot_token_files import FileBotTokenStore, TokenStoreError


def fake_token(letter="T"):
    return str(123456789) + ":" + letter * 35


@unittest.skipUnless(os.name == "posix", "POSIX-only token file store")
class TokenFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "tokens"
        self.store = FileBotTokenStore(self.root)
        self.intent = "synthetic-newbot-files-0001"

    def put(self, letter="T"):
        return self.store.store(fake_token(letter), intent_id=self.intent)

    def test_roundtrip_reopen_and_only_public_metadata(self):
        reference = self.put()
        fresh = FileBotTokenStore(self.root)
        self.assertEqual(fresh.load_for_broker(reference), fake_token())
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.root / (reference + ".json")).stat().st_mode), 0o600)
        self.assertNotIn(fake_token(), json.dumps(fresh.metadata(reference)))
        self.assertNotIn(fake_token(), reference)
        self.assertEqual(fresh.metadata(reference)["token_bot_id"], 123456789)

    def test_same_intent_returns_same_reference_without_second_file(self):
        reference = self.put()
        self.assertEqual(self.put(), reference)
        self.assertEqual(len(list(self.root.iterdir())), 1)
        self.assertEqual(self.store.for_intent(self.intent).store(fake_token()), reference)

    def test_intent_cannot_replace_another_token(self):
        reference = self.put()
        with self.assertRaisesRegex(TokenStoreError, "intent_token_conflict"):
            self.put("X")
        self.assertEqual(self.store.load_for_broker(reference), fake_token())

    def test_unsafe_existing_directory_is_not_chmodded(self):
        self.root.mkdir(mode=0o755)
        self.root.chmod(0o755)
        with self.assertRaisesRegex(TokenStoreError, "unsafe_token_directory"):
            self.put()
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o755)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_root_symlink_is_rejected(self):
        target = Path(self.temp.name) / "other"
        target.mkdir(mode=0o700)
        self.root.symlink_to(target, target_is_directory=True)
        with self.assertRaises(TokenStoreError):
            self.put()
        self.assertEqual(list(target.iterdir()), [])

    def test_leaf_symlink_and_hardlink_are_rejected(self):
        reference = self.put()
        path = self.root / (reference + ".json")
        target = Path(self.temp.name) / "preserved"
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaises(TokenStoreError):
            self.store.load_for_broker(reference)
        path.unlink()
        os.link(target, path)
        with self.assertRaisesRegex(TokenStoreError, "unsafe_token_file"):
            self.store.load_for_broker(reference)
        self.assertEqual(json.loads(target.read_text())["token"], fake_token())

    def test_world_readable_file_is_rejected_without_permission_change(self):
        reference = self.put()
        path = self.root / (reference + ".json")
        path.chmod(0o644)
        with self.assertRaisesRegex(TokenStoreError, "unsafe_token_file"):
            self.store.load_for_broker(reference)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def test_malformed_and_oversized_files_are_rejected(self):
        reference = self.put()
        path = self.root / (reference + ".json")
        for content in (b"{", b"x" * 5000, b"[]", b""):
            path.write_bytes(content)
            with self.assertRaises(TokenStoreError):
                self.store.load_for_broker(reference)

    def test_duplicate_json_members_are_rejected(self):
        reference = self.put()
        path = self.root / (reference + ".json")
        original = json.loads(path.read_text())
        path.write_text(json.dumps(original)[:-1] + ',"token":' + json.dumps(fake_token("X")) + '}')
        with self.assertRaisesRegex(TokenStoreError, "corrupt_token_file"):
            self.store.load_for_broker(reference)

    def test_reference_cannot_escape_directory(self):
        self.put()
        for reference in ("../other", "/etc/passwd", "botcred_" + "g" * 64):
            with self.assertRaisesRegex(TokenStoreError, "invalid_token_reference"):
                self.store.load_for_broker(reference)

    def test_fifo_is_rejected_without_blocking(self):
        reference = self.put()
        path = self.root / (reference + ".json")
        path.unlink()
        os.mkfifo(path, mode=0o600)
        with self.assertRaisesRegex(TokenStoreError, "unsafe_token_file"):
            self.store.load_for_broker(reference)

    def test_failed_write_keeps_claim_and_never_returns_reference(self):
        with patch("telegram_setup.newbot_token_files.os.write", side_effect=OSError("synthetic")):
            with self.assertRaisesRegex(TokenStoreError, "token_store_io_failed"):
                self.put()
        self.assertEqual(len(list(self.root.iterdir())), 1)
        with self.assertRaisesRegex(TokenStoreError, "corrupt_token_file"):
            self.put()

    def test_failed_sync_can_reconcile_same_token_without_second_file(self):
        with patch("telegram_setup.newbot_token_files.os.fsync", side_effect=OSError("synthetic")):
            with self.assertRaisesRegex(TokenStoreError, "token_store_io_failed"):
                self.put()
        reference = self.put()
        self.assertEqual(self.store.load_for_broker(reference), fake_token())
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_exception_text_never_echoes_invalid_secret(self):
        secret = fake_token() + "\n"
        with self.assertRaises(TokenStoreError) as caught:
            self.store.store(secret, intent_id=self.intent)
        self.assertNotIn(secret.strip(), str(caught.exception))
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
