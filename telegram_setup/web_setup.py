"""Loopback-only setup UI. No account secrets in URLs, logs or MCP tools."""
import argparse
import base64
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser

from .config import ROOT, connection_lock, prepare_private, private_dir
from . import permissions

ASSETS = Path(__file__).with_name('web_assets')
STATES = frozenset({'connecting', 'qr', 'phone', 'code', 'password', 'authorized', 'error'})
ERRORS = frozenset({'flood_wait', 'password_invalid', 'login_expired', 'code_invalid',
                   'code_expired', 'input_closed', 'password_attempts_exhausted',
                   'session_busy_or_account_mismatch', 'invalid_input', 'connection_failed'})


class SetupController:
    def __init__(self, *, reader_only=False):
        self.lock = threading.RLock()
        self.reader_only = reader_only
        self.event = {'state': 'ready'}
        self.process = None
        self.busy = False
        self.closed = False
        self.retry_at = 0

    def status(self):
        with self.lock:
            try:
                selected = permissions.load_settings()
            except RuntimeError:
                selected = None
            except (OSError, ValueError):
                selected = None
            return {**self.event, 'busy': self.busy, 'reader_only': self.reader_only,
                    'dependencies_ready': (ROOT / '.venv/bin/python').is_file() and (ROOT / '.upstream').is_dir(),
                    'git_available': shutil.which('git') is not None,
                    'uv_available': shutil.which('uv') is not None,
                    'api_configured': (private_dir() / 'credentials.json').is_file(),
                    'selection': selected,
                    'capabilities': [{'id': key, 'title': value[0], 'detail': value[1]}
                                     for key, value in permissions.CAPABILITIES.items()
                                     if not self.reader_only or key == 'read'],
                    'retry_after_seconds': max(0, int(self.retry_at - time.monotonic()) + 1)
                        if self.retry_at > time.monotonic() else 0}

    def set_event(self, value):
        with self.lock:
            if not self.closed:
                self.event = value

    def start_job(self, target):
        with self.lock:
            if self.busy or self.closed or self.retry_at > time.monotonic():
                raise RuntimeError('busy_or_cooldown')
            self.busy = True

        def run():
            try:
                target()
            except Exception:
                self.set_event({'state': 'error', 'code': 'operation_failed'})
            finally:
                with self.lock:
                    self.busy = False
        threading.Thread(target=run, daemon=True).start()

    def install(self, request):
        selected = permissions.validate_settings({'schema_version': 1,
            'enabled': request.get('enabled'), 'transcription_engine': request.get('transcription_engine')})
        if self.reader_only and selected['enabled'] != ['read']:
            raise ValueError('reader_only')
        if not shutil.which('git') or not shutil.which('uv'):
            raise RuntimeError('prerequisites_missing')

        def job():
            self.set_event({'state': 'installing'})
            # Preserve the running account and fail rather than changing an active backend.
            with connection_lock():
                command = [sys.executable, str(ROOT / 'bootstrap.py'), '--permissions', ','.join(selected['enabled'])]
                if selected['transcription_engine']:
                    command += ['--transcription-engine', selected['transcription_engine']]
                result = subprocess.run(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900)
                self.set_event({'state': 'installed'} if result.returncode == 0 else
                               {'state': 'error', 'code': 'installation_failed'})
        self.start_job(job)

    def login(self, request):
        if request.get('method') not in ('qr', 'phone'):
            raise ValueError('invalid_method')
        if not (ROOT / '.venv/bin/python').is_file():
            raise RuntimeError('dependencies_missing')
        if permissions.load_settings() is None:
            raise RuntimeError('selection_missing')
        if self.reader_only and permissions.load_settings()['enabled'] != ['read']:
            raise ValueError('reader_only')
        start = {key: request[key] for key in ('method', 'api_id', 'api_hash') if key in request}
        if any(not isinstance(v, str) or len(v) > 256 for v in start.values()):
            raise ValueError('invalid_input')

        def job():
            self.set_event({'state': 'connecting'})
            child = subprocess.Popen([str(ROOT / '.venv/bin/python'), '-m', 'telegram_setup.web_login'],
                cwd=ROOT, env=dict(os.environ), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1)
            try:
                with self.lock:
                    self.process = child
                    if self.closed:
                        child.terminate()
                        return
                    child.stdin.write(json.dumps(start) + '\n')
                    child.stdin.flush()
                    start.clear()
                final = False
                for line in child.stdout:
                    if len(line) > 131072:
                        break
                    event = json.loads(line)
                    state = event.get('state')
                    if state not in STATES:
                        break
                    safe = {'state': state}
                    if state == 'qr':
                        png = base64.b64decode(event.get('png', ''), validate=True)
                        if len(png) > 65536 or not png.startswith(b'\x89PNG\r\n\x1a\n'):
                            break
                        safe.update(png=base64.b64encode(png).decode('ascii'),
                                    expires_in=max(1, min(600, int(event.get('expires_in', 30)))))
                    if state == 'password':
                        safe['invalid'] = event.get('invalid') is True
                    if state == 'error':
                        safe['code'] = event.get('code') if event.get('code') in ERRORS else 'connection_failed'
                        if safe['code'] == 'flood_wait':
                            wait = max(1, int(event.get('retry_after_seconds', 60)))
                            with self.lock:
                                self.retry_at = time.monotonic() + wait
                        final = True
                    if state == 'authorized':
                        final = True
                    self.set_event(safe)
                child.wait(timeout=5)
                if not final:
                    self.set_event({'state': 'error', 'code': 'connection_failed'})
            finally:
                start.clear()
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
                for stream in (child.stdin, child.stdout):
                    stream.close()
                with self.lock:
                    self.process = None
        self.start_job(job)

    def answer(self, request):
        with self.lock:
            state = self.event.get('state')
            field = {'phone': 'phone', 'code': 'code', 'password': 'password'}.get(state)
            if not field or not self.process or self.process.poll() is not None:
                raise RuntimeError('input_not_expected')
            value = request.get(field)
            if not isinstance(value, str) or not value or len(value) > 4096:
                raise ValueError('invalid_input')
            self.process.stdin.write(json.dumps({field: value}) + '\n')
            self.process.stdin.flush()
            self.event = {'state': 'connecting'}

    def check(self):
        def job():
            self.set_event({'state': 'checking'})
            p = subprocess.run([str(ROOT / '.venv/bin/python'), str(ROOT / 'mcp_client.py'),
                                'status', '--timeout', '30'], cwd=ROOT, env=dict(os.environ),
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=45)
            try:
                result = json.loads(p.stdout)
                passed = p.returncode == 0 and result.get('connection_probe') == 'passed'
            except (ValueError, AttributeError):
                passed = False
            self.set_event({'state': 'verified'} if passed else {'state': 'error', 'code': 'mcp_check_failed'})
        if not (ROOT / '.venv/bin/python').is_file():
            raise RuntimeError('dependencies_missing')
        self.start_job(job)

    def close(self):
        with self.lock:
            self.closed = True
            self.event = {'state': 'closed'}
            if self.process and self.process.poll() is None:
                self.process.terminate()


class SetupHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, controller, port=0):
        super().__init__(('127.0.0.1', port), SetupHandler)
        self.controller = controller
        self.token = secrets.token_urlsafe(32)
        self.origin = 'http://127.0.0.1:' + str(self.server_port)

    def handle_error(self, request, client_address):
        pass  # No request bodies, headers or exceptions in access logs.


class SetupHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, status, value, content_type='application/json; charset=utf-8'):
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def permitted(self, *, api=False, write=False):
        if self.headers.get_all('Host') != [self.server.origin.removeprefix('http://')]:
            self.reply(403, {'error': 'host_rejected'})
            return False
        origin = self.headers.get('Origin')
        if (origin is not None and origin != self.server.origin) or (write and origin != self.server.origin):
            self.reply(403, {'error': 'origin_rejected'})
            return False
        if api:
            values = self.headers.get_all('Authorization') or []
            expected = 'Bearer ' + self.server.token
            if len(values) != 1 or not hmac.compare_digest(values[0], expected):
                self.reply(401, {'error': 'setup_session_required'})
                return False
        return True

    def do_GET(self):
        self.connection.settimeout(5)
        api = self.path.startswith('/api/')
        if not self.permitted(api=api):
            return
        if self.path == '/api/status':
            self.reply(200, self.server.controller.status())
            return
        selected = {'/': ('index.html', 'text/html; charset=utf-8'),
                    '/app.js': ('app.js', 'application/javascript; charset=utf-8'),
                    '/style.css': ('style.css', 'text/css; charset=utf-8')}.get(self.path)
        if selected:
            self.reply(200, (ASSETS / selected[0]).read_bytes(), selected[1])
        else:
            self.reply(404, {'error': 'not_found'})

    def do_POST(self):
        self.connection.settimeout(5)
        if not self.permitted(api=True, write=True):
            return
        if self.headers.get('Transfer-Encoding') is not None or len(self.headers.get_all('Content-Length') or []) != 1:
            self.reply(400, {'error': 'invalid_body'})
            return
        length = self.headers.get('Content-Length', '')
        if not length.isdecimal() or not 0 < int(length) <= 16384:
            self.reply(413, {'error': 'body_too_large'})
            return
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            self.reply(415, {'error': 'json_required'})
            return
        try:
            data = json.loads(self.rfile.read(int(length)))
            if not isinstance(data, dict):
                raise ValueError('invalid_input')
            action = {'/api/install': self.server.controller.install,
                      '/api/login': self.server.controller.login,
                      '/api/answer': self.server.controller.answer}.get(self.path)
            if action:
                action(data)
            elif self.path == '/api/check':
                self.server.controller.check()
            elif self.path == '/api/close':
                self.server.controller.close()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.reply(404, {'error': 'not_found'})
                return
            self.reply(202, {'accepted': True})
        except (ValueError, RuntimeError) as error:
            safe = {'busy_or_cooldown', 'prerequisites_missing', 'dependencies_missing',
                    'reader_only', 'invalid_method', 'input_not_expected'}
            self.reply(409, {'error': str(error) if str(error) in safe else 'check_input_or_active_service'})
        except Exception:
            self.reply(400, {'error': 'operation_failed'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--no-open', action='store_true')
    parser.add_argument('--open', action='store_true', help='Open the browser (default); useful for desktop launchers')
    parser.add_argument('--reader-only', action='store_true', help='Allow only the read capability in this wizard')
    parser.add_argument('--connection-file', help='New private filename inside AGENT_TELEGRAM_HOME for an owner SSH handoff')
    args = parser.parse_args()
    if args.port != 0 and not 1024 <= args.port <= 65535:
        parser.error('Use an unprivileged loopback port or 0')
    if args.no_open and not args.connection_file:
        parser.error('--no-open requires --connection-file for the private owner handoff')
    prepare_private()
    controller = SetupController(reader_only=args.reader_only)
    server = SetupHTTPServer(controller, args.port)
    url = server.origin + '/#setup=' + server.token
    handoff = None
    handoff_fd = None
    timer = None
    try:
        if args.connection_file:
            candidate = Path(args.connection_file).expanduser().absolute()
            if candidate.parent.resolve() != private_dir() or candidate.is_symlink():
                parser.error('--connection-file must be a new file in the private installation directory')
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW |
                         getattr(os, 'O_CLOEXEC', 0), 0o600)
            # Only remove a handoff created by this invocation; an existing
            # file must survive validation or O_EXCL failures unchanged. Keep
            # the descriptor open so a removed inode cannot be reused while
            # the wizard is running.
            handoff, handoff_fd = candidate, fd
            data = json.dumps({'url': url, 'expires_after_seconds': 1800}).encode()
            while data:
                count = os.write(handoff_fd, data)
                if count <= 0:
                    raise OSError('handoff_write_failed')
                data = data[count:]
            os.fsync(handoff_fd)
        # This one-time URL is shown only in the owner's browser; never print it.
        if not args.no_open:
            try:
                opened = webbrowser.open(url)
            except Exception:
                opened = False
            if not opened:
                print('Could not open a local browser. Start the setup again after checking the browser.',
                      file=sys.stderr)
                return 1
        print('Local Telegram setup is ready on port ' + str(server.server_port) + '. No secrets are logged.', flush=True)
        timer = threading.Timer(1800, server.shutdown)
        timer.daemon = True
        timer.start()
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        if timer is not None:
            timer.cancel()
        controller.close()
        server.server_close()
        if handoff_fd is not None:
            try:
                current = handoff.lstat()
                original = os.fstat(handoff_fd)
                if (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino):
                    handoff.unlink()
            except FileNotFoundError:
                pass
            finally:
                os.close(handoff_fd)
    return 0
