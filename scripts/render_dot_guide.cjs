// Optional maintainer export. Requires sharp; no browser or account access.
'use strict';
const fs = require('node:fs/promises');
const path = require('node:path');
const sharp = require('sharp');
const root = path.resolve(__dirname, '..');

function pixelsOnly(png) {
  const chunks = [png.subarray(0, 8)];
  for (let offset = 8; offset < png.length;) {
    const length = png.readUInt32BE(offset), end = offset + length + 12;
    const kind = png.toString('ascii', offset + 4, offset + 8);
    if (end > png.length) throw new Error('Invalid generated PNG');
    if (['IHDR', 'IDAT', 'IEND'].includes(kind)) chunks.push(png.subarray(offset, end));
    offset = end;
  }
  return Buffer.concat(chunks);
}

async function main() {
  const manifest = JSON.parse(await fs.readFile(path.join(root, 'docs/dot-guide-steps.json'), 'utf8'));
  for (const step of manifest.steps) {
    const svg = await fs.readFile(path.join(root, 'docs', step.editable_source));
    const png = await sharp(svg).png({compressionLevel: 9}).toBuffer();
    await fs.writeFile(path.join(root, 'docs', step.image), pixelsOnly(png), {mode: 0o644});
  }
  process.stdout.write('Rendered 8 public teaching PNGs without embedded text metadata.\n');
}

main().catch(() => {process.stderr.write('Guide image export failed.\n'); process.exitCode = 1;});
