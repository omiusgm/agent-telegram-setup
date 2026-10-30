"""Serve the real setup page with fake, read-only state for guide screenshots.

This fixture never opens a Telegram session, stores credentials, runs an
installer, or contacts the Internet. It is not an installation wizard.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from telegram_setup import permissions, web_setup  # noqa: E402

FAKE_TOKEN = 'synthetic-preview-only'


class FakeController:
    def __init__(self, phase): self.phase = phase

    def status(self):
        ready = self.phase != 'capabilities'
        return {'state': 'verified' if self.phase == 'verified' else 'ready',
                'busy': False, 'reader_only': False,
                'dependencies_ready': ready, 'git_available': True, 'uv_available': True,
                'api_configured': self.phase == 'verified',
                'selection': {'schema_version': 1, 'enabled': ['read'], 'transcription_engine': None} if ready else None,
                'capabilities': [{'id': k, 'title': v[0], 'detail': v[1]} for k, v in permissions.CAPABILITIES.items()],
                'retry_after_seconds': 0}

    def install(self, _): raise RuntimeError('preview_only')
    def login(self, _): raise RuntimeError('preview_only')
    def answer(self, _): raise RuntimeError('preview_only')
    def check(self): raise RuntimeError('preview_only')
    def close(self): pass


class PreviewHandler(web_setup.SetupHandler):
    def do_GET(self):
        if self.path == '/':
            if not self.permitted(): return
            content = (web_setup.ASSETS / 'index.html').read_text(encoding='utf-8')
            content = content.replace('<body>', '<body><div class="preview-banner">'
                'УЧЕБНЫЙ ПРИМЕР · ВЫМЫШЛЕННЫЕ ДАННЫЕ · НЕ ПОДКЛЮЧЕНИЕ</div>', 1)
            self.reply(200, content.encode(), 'text/html; charset=utf-8')
        elif self.path == '/style.css':
            if not self.permitted(): return
            content = (web_setup.ASSETS / 'style.css').read_bytes()
            content += (b'\n.preview-banner{max-width:1160px;margin:0 auto 12px;padding:8px 12px;'
                        b'background:#fff2cd;color:#513900;border-radius:8px;text-align:center;font-weight:700}\n')
            self.reply(200, content, 'text/css; charset=utf-8')
        else:
            super().do_GET()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('capabilities', 'login', 'verified'), required=True)
    args = parser.parse_args()
    server = web_setup.SetupHTTPServer(FakeController(args.phase))
    server.RequestHandlerClass = PreviewHandler
    server.token = FAKE_TOKEN
    print(json.dumps({'origin': server.origin, 'synthetic': True}), flush=True)
    try: server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == '__main__': main()
