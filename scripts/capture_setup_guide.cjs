// Optional maintainer tool. Uses an existing Playwright, sharp and browser;
// never downloads them and only connects to a synthetic loopback fixture.
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');
const { chromium } = require('playwright');
const sharp = require('sharp');

const root = path.resolve(__dirname, '..');
const browserPath = process.argv[2];
if (!browserPath) throw new Error('Pass the path to an already installed Chromium browser');
const output = path.join(root, 'docs', 'assets', 'start-here');
fs.mkdirSync(output, { recursive: true });

function withoutMetadata(png) {
  const signature = Buffer.from('89504e470d0a1a0a', 'hex');
  if (!png.subarray(0, 8).equals(signature)) throw new Error('Invalid synthetic PNG');
  const chunks = [signature]; let offset = 8, ended = false;
  while (offset + 12 <= png.length) {
    const length = png.readUInt32BE(offset), end = offset + length + 12;
    if (end > png.length) throw new Error('Truncated synthetic PNG');
    const kind = png.toString('ascii', offset + 4, offset + 8);
    if (['IHDR', 'IDAT', 'IEND'].includes(kind)) chunks.push(png.subarray(offset, end));
    offset = end;
    if (kind === 'IEND') { ended = true; break; }
  }
  if (!ended || offset !== png.length) throw new Error('Invalid synthetic PNG end');
  return Buffer.concat(chunks);
}

async function fixture(phase) {
  const child = spawn(process.env.TELEGRAM_SETUP_PREVIEW_PYTHON || 'python3',
    [path.join(__dirname, 'fake_setup_preview.py'), '--phase', phase],
    { cwd: root, env: { PATH: process.env.PATH, LANG: 'C.UTF-8', PYTHONDONTWRITEBYTECODE: '1' },
      stdio: ['ignore', 'pipe', 'ignore'] });
  const lines = readline.createInterface({ input: child.stdout });
  const first = await new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('exit', () => reject(new Error('Synthetic preview exited early')));
    lines.once('line', resolve);
  });
  const value = JSON.parse(first);
  if (value.synthetic !== true || !/^http:\/\/127\.0\.0\.1:\d+$/.test(value.origin)) {
    child.kill(); throw new Error('Invalid synthetic preview');
  }
  return { child, origin: value.origin };
}

async function capture(browser, phase, filename) {
  const server = await fixture(phase);
  const context = await browser.newContext({ viewport: { width: 1200, height: 800 }, deviceScaleFactor: 1 });
  try {
    await context.route('**/*', route => {
      const url = route.request().url();
      return url.startsWith(server.origin + '/') ? route.continue() : route.abort();
    });
    const page = await context.newPage();
    await page.goto(server.origin + '/#setup=synthetic-preview-only', { waitUntil: 'networkidle' });
    await page.waitForSelector('.preview-banner');
    await page.waitForSelector(phase === 'capabilities' ? '#capabilities input'
      : phase === 'login' ? '#api-id' : '#complete:not([hidden])');
    const image = await page.screenshot({ fullPage: false });
    // Keep only PNG image chunks: no browser/EXIF or ancillary metadata.
    const png = await sharp(image).png().toBuffer();
    fs.writeFileSync(path.join(output, filename), withoutMetadata(png));
  } finally {
    await context.close(); server.child.kill('SIGTERM');
  }
}

(async () => {
  const browser = await chromium.launch({ executablePath: browserPath, headless: true,
    args: ['--disable-background-networking', '--disable-component-update', '--disable-sync', '--no-first-run'] });
  try {
    await capture(browser, 'capabilities', '01-capabilities.png');
    await capture(browser, 'login', '02-login.png');
    await capture(browser, 'verified', '03-check.png');
  } finally { await browser.close(); }
  console.log('Three synthetic setup screenshots created; no account data was used.');
})().catch(() => { console.error('Synthetic setup screenshot capture failed.'); process.exitCode = 1; });
