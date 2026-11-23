"""Synthetic browser test; uses an installed Chrome, never a Telegram backend."""
import html
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ASSETS = Path(__file__).resolve().parents[1] / 'telegram_setup' / 'web_assets'
CHROME = os.environ.get('CHROME_BIN') or shutil.which('chromium') or shutil.which('google-chrome') or '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

# The page receives only synthetic state. Fetch is intercepted before app.js runs.
HARNESS = r'''<script>
const posts = [];
const status = {state:'ready', busy:false, reader_only:false, dependencies_ready:true,
 git_available:true, uv_available:true, api_configured:false, retry_after_seconds:0,
 selection:{enabled:['read'], transcription_engine:null}, capabilities:[]};
window.fetch = async (url, options={}) => {
 if (options.method === 'POST') posts.push({url, body:JSON.parse(options.body)});
 return {ok:true, json:async()=>status};
};
addEventListener('DOMContentLoaded', async () => {
 const check = (condition, message) => { if (!condition) throw Error(message); };
 const wait = () => new Promise(resolve => setTimeout(resolve, 100));
 const id = document.querySelector('#api-id'), hash = document.querySelector('#api-hash');
 const file = document.querySelector('#api-file');
 const syntheticId = '987654', syntheticHash = 'abcdef0123456789'.repeat(2);
 const choose = async (text, name = 'synthetic.env') => {
   const transfer = new DataTransfer();
   const chosen = new File([text], name, {type:'text/plain'});
   await chosen.text();
   transfer.items.add(chosen);
   file.files = transfer.files; file.dispatchEvent(new Event('change', {bubbles:true}));
   for (let n=0; n<50 && file.files.length; n++) await wait();
 };
 const assertPrivate = () => {
   check(posts.length === 0, 'POST before explicit login');
   const visible = document.body.innerText;
   check(!visible.includes(syntheticId) && !visible.includes(syntheticHash), 'credentials echoed in page text');
   check(id.type === 'password' && hash.type === 'password', 'credential fields are not masked');
 };
 const qr = async () => { document.querySelector('#login-qr').click(); await wait(); };
 const expected = () => check(posts.at(-1)?.url === '/api/login' &&
   JSON.stringify(posts.at(-1).body) === JSON.stringify({method:'qr',api_id:syntheticId,api_hash:syntheticHash}), 'incorrect login payload');
 try {
   await wait();
   check(file && file.type === 'file', 'file input missing');
   await choose('API_ID=oops\nAPI_HASH=bad');
   check(!id.value && !hash.value, 'malformed file accepted'); assertPrivate();
   await choose('API_ID=' + syntheticId + '\nAPI_HASH=' + syntheticHash + '\n' + 'x'.repeat(1048576));
   check(!id.value && !hash.value, 'oversized file accepted'); assertPrivate();
   await choose('API_ID=' + syntheticId + '\nAPI_HASH=' + syntheticHash + '\n');
   check(id.value === syntheticId && hash.value === syntheticHash, 'valid env file not imported'); assertPrivate();
   id.value=''; hash.value='';
   await choose('API_ID=' + syntheticId + '\nAPI_HASH=' + syntheticHash + '\n', 'synthetic.tgapp');
   check(id.value === syntheticId && hash.value === syntheticHash, 'valid tgapp file not imported'); assertPrivate();
   await qr(); check(posts.length === 1, 'QR did not send one request'); expected();
   id.value=syntheticId; hash.value=syntheticHash; await qr();
   check(posts.length === 2, 'manual entry did not send'); expected();
   const clipboard = new DataTransfer(); clipboard.setData('text/plain', 'API_ID=' + syntheticId + '\nAPI_HASH=' + syntheticHash);
   id.dispatchEvent(new ClipboardEvent('paste', {clipboardData:clipboard, bubbles:true, cancelable:true}));
   check(id.value === syntheticId && hash.value === syntheticHash, 'paste did not fill fields');
   await qr(); check(posts.length === 3, 'paste did not send'); expected();
   document.body.dataset.testResult='PASS';
 } catch (error) { document.body.dataset.testResult='FAIL: ' + error.message; }
});
</script>'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def do_GET(self):
        if self.path == '/delay':
            time.sleep(3)
            self.send_response(204)
            self.end_headers()
            return
        name = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}.get(self.path)
        if not name:
            self.send_error(404)
            return
        content = (ASSETS / name).read_bytes()
        if name == 'index.html':
            content = content.replace(b'<head>', b'<head>' + HARNESS.encode() + b'<img src="/delay">', 1)
        self.send_response(200)
        self.send_header('Content-Type', {'index.html': 'text/html; charset=utf-8', 'app.js': 'text/javascript', 'style.css': 'text/css'}[name])
        self.end_headers()
        self.wfile.write(content)


class EnvImportBrowserTest(unittest.TestCase):
    @unittest.skipUnless(Path(CHROME).is_file(), 'installed Chrome or Chromium required; set CHROME_BIN')
    def test_synthetic_import_and_existing_paths(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as profile:
                result = subprocess.run([CHROME, '--headless', '--disable-gpu', '--no-first-run',
                    '--disable-background-networking', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
                    '--user-data-dir=' + profile, '--dump-dom',
                    f'http://127.0.0.1:{server.server_port}/#setup=synthetic'],
                    capture_output=True, text=True, timeout=30, check=True)
            from re import search
            match = search(r'data-test-result="([^"]*)"', result.stdout)
            self.assertIsNotNone(match, 'browser test did not finish')
            self.assertEqual(html.unescape(match.group(1)), 'PASS')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__': unittest.main()
