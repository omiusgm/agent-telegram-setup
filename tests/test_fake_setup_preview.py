"""The guide preview serves only synthetic state and rejects setup mutations."""
import http.client
import importlib.util
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('fake_setup_preview', ROOT / 'scripts/fake_setup_preview.py')
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)


class FakePreviewTests(unittest.TestCase):
    def setUp(self):
        self.private = patch.object(fixture.web_setup, 'private_dir', side_effect=AssertionError('real private state used'))
        self.private.start()
        self.server = fixture.web_setup.SetupHTTPServer(fixture.FakeController('login'))
        self.server.RequestHandlerClass = fixture.PreviewHandler
        self.server.token = fixture.FAKE_TOKEN
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2); self.private.stop()

    def request(self, path, payload=None):
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        headers = {'Authorization': 'Bearer ' + fixture.FAKE_TOKEN}
        if payload is not None:
            headers.update({'Origin': self.server.origin, 'Content-Type': 'application/json'})
        client.request('POST' if payload is not None else 'GET', path,
                       body=json.dumps(payload) if payload is not None else None, headers=headers)
        response = client.getresponse(); result = response.status, response.read(); client.close(); return result

    def test_preview_is_clearly_labelled_and_does_not_show_keys(self):
        status, body = self.request('/')
        self.assertEqual(status, 200)
        self.assertIn('УЧЕБНЫЙ ПРИМЕР'.encode(), body)
        self.assertNotIn(fixture.FAKE_TOKEN.encode(), body)
        status, body = self.request('/api/status')
        self.assertEqual(status, 200); data = json.loads(body)
        self.assertFalse(data['api_configured']); self.assertNotIn('api_hash', data)

    def test_preview_rejects_login_and_install(self):
        for route, value in (('/api/login', {'method': 'qr'}),
                             ('/api/install', {'enabled': ['read']}), ('/api/check', {})):
            status, _ = self.request(route, value)
            self.assertEqual(status, 409)


if __name__ == '__main__': unittest.main()
