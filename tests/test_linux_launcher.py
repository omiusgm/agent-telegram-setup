"""Linux launcher creation with fake Python; no desktop or account is used."""
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('create_linux_launcher', ROOT / 'scripts/create_linux_launcher.py')
launcher = importlib.util.module_from_spec(spec); spec.loader.exec_module(launcher)


class LinuxLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.root = Path(self.temporary.name)
        self.python = self.root / 'fake-python3'
        self.python.write_text('#!/bin/sh\nexit 0\n'); self.python.chmod(0o700)
        self.target = self.root / 'Telegram-Setup.desktop'

    def tearDown(self): self.temporary.cleanup()

    def run_main(self, *extra):
        args = ['create_linux_launcher.py', '--output', str(self.target), *extra]
        with patch.object(sys, 'argv', args), patch.object(sys, 'platform', 'linux'), \
             patch.object(launcher, 'ROOT', self.root), patch.object(launcher.shutil, 'which', return_value=str(self.python)), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return launcher.main()

    def test_supported_python_creates_private_reader_launcher(self):
        self.run_main('--reader-only')
        content = self.target.read_text()
        self.assertIn('--reader-only', content); self.assertIn('Terminal=false', content)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o700)

    def test_unsupported_python_does_not_create_launcher(self):
        self.python.write_text('#!/bin/sh\nexit 1\n')
        with self.assertRaises(SystemExit): self.run_main()
        self.assertFalse(self.target.exists())

    def test_existing_launcher_is_not_overwritten(self):
        self.target.write_text('existing')
        with self.assertRaises(SystemExit): self.run_main()
        self.assertEqual(self.target.read_text(), 'existing')


if __name__ == '__main__': unittest.main()
