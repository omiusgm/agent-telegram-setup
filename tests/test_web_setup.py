"""Browser setup: loopback/CSRF boundaries and real private pipe lifecycle."""
import asyncio
import base64
import contextlib
from datetime import datetime, timedelta, timezone
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import sys
from unittest.mock import AsyncMock, patch

from telethon import errors
from telegram_setup import web_setup, web_login
from telegram_setup.permissions import save_settings


PNG = base64.b64encode(b'\x89PNG\r\n\x1a\nsynthetic-image').decode('ascii')
FAKE_WORKER = '''
import json, sys
def emit(state, **values):
    print(json.dumps(dict(state=state, **values)), flush=True)
request = json.loads(sys.stdin.readline())
assert request['method'] == 'qr'
emit('qr', png=PNG, expires_in=60)
emit('password', invalid=False)
password = json.loads(sys.stdin.readline())['password']
assert password == 'synthetic-owner-input'
emit('authorized')
'''


class BrowserBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / 'private'
        self.env = patch.dict(os.environ, {'AGENT_TELEGRAM_HOME': str(self.home)}, clear=True)
        self.env.start()
        self.root_patch = patch.object(web_setup, 'ROOT', self.root)
        self.root_patch.start()
        save_settings({'schema_version': 1, 'enabled': ['read'], 'transcription_engine': None})
        self.controller = web_setup.SetupController(reader_only=True)
        self.server = web_setup.SetupHTTPServer(self.controller)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.controller.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        deadline = time.monotonic() + 3
        while self.controller.busy and time.monotonic() < deadline:
            time.sleep(.02)
        self.root_patch.stop()
        self.env.stop()
        self.temporary.cleanup()

    def request(self, path, body=None, *, auth=True, origin=None, host=None):
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        headers = {}
        if auth:
            headers['Authorization'] = 'Bearer ' + self.server.token
        if origin is not None:
            headers['Origin'] = origin
        if host is not None:
            headers['Host'] = host
        if body is not None:
            headers['Content-Type'] = 'application/json'
        client.request('POST' if body is not None else 'GET', path,
                       body=json.dumps(body) if body is not None else None, headers=headers)
        response = client.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        client.close()
        return result

    def wait_for(self, state):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = self.controller.status()
            if current['state'] == state:
                return current
            time.sleep(.02)
        self.fail('Worker did not reach expected state: ' + state)

    def test_loopback_auth_origin_host_body_limit_and_no_secret_status(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        self.assertEqual(self.request('/api/status', auth=False)[0], 401)
        self.assertEqual(self.request('/api/check', {}, origin='https://example.invalid')[0], 403)
        self.assertEqual(self.request('/api/check', {})[0], 403)
        self.assertEqual(self.request('/api/status', host='rebinding.example.invalid')[0], 403)
        self.assertEqual(self.request('/api/login', {'x': 'a' * 17000}, origin=self.server.origin)[0], 413)
        code, headers, body = self.request('/api/status')
        self.assertEqual(code, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertNotIn(self.server.token.encode(), body)
        self.assertNotIn(b'api_hash', body)
        self.assertFalse(self.controller.busy)
        # Unknown URLs cannot expose files, even to a locally authenticated owner.
        self.assertEqual(self.request('/../credentials.json')[0], 404)

    def test_private_worker_password_round_trip_and_qr_removed(self):
        binary = self.root / '.venv/bin/python'
        binary.parent.mkdir(parents=True)
        import sys
        binary.symlink_to(sys.executable)
        package = self.root / 'telegram_setup'
        package.mkdir()
        (package / '__init__.py').touch()
        (package / 'web_login.py').write_text('PNG = ' + repr(PNG) + '\n' + FAKE_WORKER)
        self.assertEqual(self.request('/api/login', {'method': 'qr'}, origin=self.server.origin)[0], 202)
        status = self.wait_for('password')
        self.assertNotIn('png', status)
        self.assertEqual(self.request('/api/login', {'method': 'qr'}, origin=self.server.origin)[0], 409)
        self.assertEqual(self.request('/api/answer', {'password': 'synthetic-owner-input'},
                                      origin=self.server.origin)[0], 202)
        status = self.wait_for('authorized')
        self.assertNotIn('synthetic-owner-input', json.dumps(status))
        self.assertNotIn('png', status)
        self.assertFalse((self.home / 'credentials.json').exists(), 'The fixture must not create a real account')

    def test_reader_wizard_cannot_enable_writes(self):
        with patch.object(web_setup.shutil, 'which', return_value='synthetic-executable'):
            code, _, _ = self.request('/api/install', {'enabled': ['read', 'send-text'],
                                                       'transcription_engine': None}, origin=self.server.origin)
        self.assertEqual(code, 409)
        self.assertFalse(self.controller.busy)


class BrowserStartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / 'private'
        self.env = patch.dict(os.environ, {'AGENT_TELEGRAM_HOME': str(self.home)})
        self.env.start()

    def tearDown(self):
        self.env.stop(); self.temporary.cleanup()

    def test_browser_failure_stops_cleanly_without_printing_handoff_or_token(self):
        handoff = self.home / 'handoff.json'
        created = []
        original = web_setup.SetupHTTPServer
        def server(*args, **kwargs):
            result = original(*args, **kwargs); created.append(result); return result
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['setup_web.py', '--connection-file', str(handoff)]), \
             patch.object(original, 'serve_forever') as serve, \
             patch.object(web_setup, 'SetupHTTPServer', side_effect=server), \
             patch.object(web_setup.webbrowser, 'open', return_value=False) as browser, \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(web_setup.main(), 1)
        serve.assert_not_called(); browser.assert_called_once()
        self.assertFalse(handoff.exists()); self.assertEqual(created[0].socket.fileno(), -1)
        self.assertIn('Could not open a local browser', stderr.getvalue())
        self.assertNotIn(created[0].token, stdout.getvalue() + stderr.getvalue())

    def test_browser_exception_is_redacted_and_does_not_leave_handoff(self):
        handoff = self.home / 'handoff.json'
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['setup_web.py', '--connection-file', str(handoff)]), \
             patch.object(web_setup.webbrowser, 'open', side_effect=RuntimeError('synthetic-secret')), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(web_setup.main(), 1)
        self.assertFalse(handoff.exists())
        self.assertNotIn('synthetic-secret', stdout.getvalue() + stderr.getvalue())

    def test_existing_handoff_is_never_deleted_when_creation_fails(self):
        self.home.mkdir(mode=0o700)
        handoff = self.home / 'handoff.json'; handoff.write_text('existing owner file')
        with patch.object(sys, 'argv', ['setup_web.py', '--connection-file', str(handoff)]), \
             patch.object(web_setup.webbrowser, 'open') as browser:
            with self.assertRaises(FileExistsError): web_setup.main()
        browser.assert_not_called(); self.assertEqual(handoff.read_text(), 'existing owner file')

    def test_headless_mode_does_not_open_browser_and_cleans_handoff(self):
        handoff = self.home / 'handoff.json'; stdout = io.StringIO()
        with patch.object(sys, 'argv', ['setup_web.py', '--no-open', '--connection-file', str(handoff)]), \
             patch.object(web_setup.SetupHTTPServer, 'serve_forever') as serve, \
             patch.object(web_setup.webbrowser, 'open') as browser, contextlib.redirect_stdout(stdout):
            self.assertEqual(web_setup.main(), 0)
        serve.assert_called_once(); browser.assert_not_called(); self.assertFalse(handoff.exists())
        self.assertNotIn('#setup=', stdout.getvalue())

    def test_headless_mode_requires_a_private_handoff_before_opening_a_server(self):
        stderr = io.StringIO()
        with patch.object(sys, 'argv', ['setup_web.py', '--no-open']), \
             patch.object(web_setup, 'SetupHTTPServer') as server, contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit): web_setup.main()
        server.assert_not_called()
        self.assertIn('--connection-file', stderr.getvalue())
        self.assertFalse(self.home.exists())

    def test_replaced_handoff_is_not_removed_when_the_wizard_closes(self):
        handoff = self.home / 'handoff.json'
        def replace(_server, **_kwargs):
            handoff.unlink(); handoff.write_text('replacement owner file')
        with patch.object(sys, 'argv', ['setup_web.py', '--no-open', '--connection-file', str(handoff)]), \
             patch.object(web_setup.SetupHTTPServer, 'serve_forever', new=replace), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(web_setup.main(), 0)
        self.assertEqual(handoff.read_text(), 'replacement owner file')


class LoginWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_password_retry_never_emits_secret(self):
        events = []
        client = type('Client', (), {'sign_in': AsyncMock(side_effect=[errors.PasswordHashInvalidError(None), None])})()
        with patch.object(web_login, 'receive', AsyncMock(side_effect=['synthetic-wrong', 'synthetic-right'])), \
                patch.object(web_login, 'emit', side_effect=lambda state, **values: events.append((state, values))):
            await web_login.password_step(client)
        self.assertEqual(events, [('password', {'invalid': False}), ('password', {'invalid': True})])
        self.assertNotIn('synthetic-', json.dumps(events))

    async def test_qr_listener_refresh_and_two_factor(self):
        class QR:
            expires = datetime.now(timezone.utc) + timedelta(seconds=60)
            url = 'tg://login?token=synthetic-expiring-fixture'
            calls = 0
            refreshes = 0
            async def wait(self, timeout):
                self.calls += 1
                if self.calls == 1:
                    raise asyncio.TimeoutError()
                raise errors.SessionPasswordNeededError(None)
            async def recreate(self):
                self.refreshes += 1
        qr = QR()
        events = []
        def emit(state, **values):
            self.assertGreater(qr.calls, 0, 'Subscribe before making the QR visible')
            events.append((state, values))
        client = type('Client', (), {'qr_login': AsyncMock(return_value=qr)})()
        with patch.object(web_login, 'emit', side_effect=emit), \
                patch.object(web_login, 'password_step', AsyncMock()) as password:
            await web_login.qr_step(client)
        self.assertEqual(qr.refreshes, 1)
        password.assert_awaited_once_with(client)
        self.assertEqual(len(events), 2)
        self.assertTrue(all(base64.b64decode(value['png']).startswith(b'\x89PNG') for _, value in events))

    async def test_phone_code_and_two_factor_use_the_same_client(self):
        client = type('Client', (), {'send_code_request': AsyncMock(),
                                    'sign_in': AsyncMock(side_effect=errors.SessionPasswordNeededError(None))})()
        with patch.object(web_login, 'emit'), \
                patch.object(web_login, 'receive', AsyncMock(side_effect=['+' + '1' * 11, '12345'])), \
                patch.object(web_login, 'password_step', AsyncMock()) as password:
            await web_login.phone_step(client)
        client.send_code_request.assert_awaited_once()
        password.assert_awaited_once_with(client)


if __name__ == '__main__':
    unittest.main()
