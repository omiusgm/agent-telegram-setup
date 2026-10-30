"""Private owner-input helper: synthetic values, no network or real key."""
import json
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import warnings
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/save_tunnel_key.py'
spec = importlib.util.spec_from_file_location('save_tunnel_key', SCRIPT)
helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)
SYNTHETIC = b'synthetic-runtime-value-' + b'A' * 40


class SaveTunnelKeyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.directory = self.root / 'private-credentials'

    def tearDown(self): self.temporary.cleanup()

    def run_helper(self, value, directory=None, *extra):
        command = [sys.executable, str(SCRIPT), '--directory', str(directory or self.directory), *extra]
        return subprocess.run(command, input=value, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)

    def test_pipe_saves_once_in_private_file_without_printing_value(self):
        result = self.run_helper(SYNTHETIC + b'\n')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['status'], 'saved')
        self.assertNotIn(SYNTHETIC, result.stdout + result.stderr)
        file = self.directory / 'runtime.token'
        self.assertEqual(file.read_bytes(), SYNTHETIC)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        again = self.run_helper(b'another-synthetic-value-' + b'B' * 32)
        self.assertNotEqual(again.returncode, 0)
        self.assertEqual(json.loads(again.stdout)['reason'], 'already_exists')
        self.assertEqual(file.read_bytes(), SYNTHETIC)

    def test_invalid_or_multiple_values_do_not_create_a_key(self):
        for value in (b'', b'abc', SYNTHETIC + b'\n' + SYNTHETIC, b'{' + SYNTHETIC + b'}',
                      b'A' * 4097, SYNTHETIC + b'\x00'):
            result = self.run_helper(value)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(value, result.stdout + result.stderr) if value else None
            self.assertFalse((self.directory / 'runtime.token').exists())

    def test_unsafe_directory_or_symlink_is_not_changed(self):
        self.directory.mkdir(mode=0o755); self.directory.chmod(0o755)
        result = self.run_helper(SYNTHETIC)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o755)
        self.assertFalse((self.directory / 'runtime.token').exists())
        self.directory.rmdir()
        target = self.root / 'external'; target.mkdir(mode=0o700)
        self.directory.symlink_to(target, target_is_directory=True)
        result = self.run_helper(SYNTHETIC)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(target.iterdir()), [])

    def test_existing_key_symlink_is_not_followed(self):
        self.directory.mkdir(mode=0o700)
        target = self.root / 'existing'; target.write_text('unchanged')
        (self.directory / 'runtime.token').symlink_to(target)
        result = self.run_helper(SYNTHETIC)
        self.assertNotEqual(result.returncode, 0); self.assertEqual(target.read_text(), 'unchanged')

    def test_git_checkout_is_rejected(self):
        checkout = self.root / 'repo'; checkout.mkdir(mode=0o700)
        (checkout / '.git').mkdir()
        result = self.run_helper(SYNTHETIC, checkout / 'secret-dir')
        self.assertNotEqual(result.returncode, 0); self.assertFalse((checkout / 'secret-dir').exists())

    def test_world_writable_parent_is_rejected_without_changing_it(self):
        self.root.chmod(0o777)
        try:
            result = self.run_helper(SYNTHETIC)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o777)
            self.assertFalse(self.directory.exists())
        finally:
            self.root.chmod(0o700)

    def test_bad_arguments_do_not_echo_accidentally_pasted_value(self):
        result = self.run_helper(b'', self.directory, SYNTHETIC.decode())
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(SYNTHETIC, result.stdout + result.stderr)
        self.assertFalse(self.directory.exists())

    def test_failed_fsync_removes_only_the_file_created_by_this_attempt(self):
        # First fsync persists the new directory; the file fsync then fails.
        with patch.object(helper.os, 'fsync', side_effect=[None, OSError('synthetic filesystem failure')]):
            with self.assertRaises(OSError): helper.save_new(str(self.directory), SYNTHETIC)
        self.assertFalse((self.directory / 'runtime.token').exists())

    def test_tty_uses_hidden_getpass_and_rejects_echo_fallback(self):
        with patch.object(helper.sys.stdin, 'isatty', return_value=True), \
             patch.object(helper.getpass, 'getpass', return_value=SYNTHETIC.decode()) as getpass_input:
            self.assertEqual(helper.read_key(), SYNTHETIC)
            getpass_input.assert_called_once()
        def insecure_fallback(_):
            warnings.warn('synthetic echo warning', helper.getpass.GetPassWarning)
        with patch.object(helper.sys.stdin, 'isatty', return_value=True), \
             patch.object(helper.getpass, 'getpass', side_effect=insecure_fallback):
            with self.assertRaises(helper.getpass.GetPassWarning): helper.read_key()


if __name__ == '__main__': unittest.main()
