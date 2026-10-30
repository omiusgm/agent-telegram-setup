"""Launcher regression checks using fake Python and a fake local alert command."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'TelegramSetup.app/Contents/MacOS/run-setup'


class MacLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.launcher = self.root / 'TelegramSetup.app/Contents/MacOS/run-setup'
        self.launcher.parent.mkdir(parents=True)
        self.alert = self.root / 'fake-osascript'
        self.alert.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$ALERT_CAPTURE"\n')
        self.alert.chmod(0o700)
        # Substitute the absolute OS alert program only inside this fake copy;
        # the actual launcher always uses macOS's /usr/bin/osascript.
        self.launcher.write_text(SOURCE.read_text().replace('/usr/bin/osascript', shlex.quote(str(self.alert))))
        self.launcher.chmod(0o700)
        self.python = self.root / '.venv/bin/python'; self.python.parent.mkdir(parents=True)
        self.python.write_text(
            '#!/bin/sh\n'
            'if [ "$1" = "-c" ]; then [ "$FAKE_VERSION_OK" = 1 ]; exit $?; fi\n'
            'printf launched > "$LAUNCH_CAPTURE"\n'
            '[ "$FAKE_LAUNCH_OK" = 1 ]; exit $?\n')
        self.python.chmod(0o700)
        self.alert_capture = self.root / 'alert.txt'; self.launch_capture = self.root / 'launched.txt'
        self.env = dict(os.environ, ALERT_CAPTURE=str(self.alert_capture), LAUNCH_CAPTURE=str(self.launch_capture),
                        FAKE_VERSION_OK='1', FAKE_LAUNCH_OK='1')

    def tearDown(self): self.temporary.cleanup()

    def run_launcher(self):
        return subprocess.run(['/bin/sh', str(self.launcher)], env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)

    def wait_for(self, path):
        end = time.monotonic() + 3
        while not path.exists() and time.monotonic() < end: time.sleep(.01)
        self.assertTrue(path.exists())

    def test_unsupported_python_explains_how_to_continue(self):
        self.env['FAKE_VERSION_OK'] = '0'
        result = self.run_launcher()
        self.assertNotEqual(result.returncode, 0); self.wait_for(self.alert_capture)
        self.assertIn('Python 3.11', self.alert_capture.read_text())
        self.assertFalse(self.launch_capture.exists())

    def test_failed_browser_or_setup_shows_safe_alert(self):
        self.env['FAKE_LAUNCH_OK'] = '0'
        self.assertEqual(self.run_launcher().returncode, 0)
        self.wait_for(self.alert_capture); self.wait_for(self.launch_capture)
        self.assertIn('Мастер Telegram не открылся', self.alert_capture.read_text())

    def test_successful_launch_does_not_raise_an_alert(self):
        self.assertEqual(self.run_launcher().returncode, 0)
        self.wait_for(self.launch_capture)
        self.assertFalse(self.alert_capture.exists())


if __name__ == '__main__': unittest.main()
