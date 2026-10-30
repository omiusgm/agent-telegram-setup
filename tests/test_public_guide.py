"""The published synthetic screenshots must remain metadata-free PNGs."""
import importlib.util
from pathlib import Path
import re
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zlib

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('check_public', ROOT / 'scripts/check_public.py')
check = importlib.util.module_from_spec(spec); spec.loader.exec_module(check)


class PublicGuideTests(unittest.TestCase):
    def test_three_synthetic_screenshots_have_expected_dimensions_and_no_metadata(self):
        for name in ('01-capabilities.png', '02-login.png', '03-check.png'):
            path = ROOT / 'docs/assets/start-here' / name
            self.assertEqual(check.check_png(path.read_bytes()), (1200, 800))
            self.assertIn('docs/assets/start-here/' + name, check.PUBLIC_FILES)

    def test_embedded_text_metadata_is_rejected(self):
        path = ROOT / 'docs/assets/start-here/01-capabilities.png'
        image = path.read_bytes(); payload = b'comment\0synthetic-private-data'
        chunk = b'tEXt' + payload
        injected = (struct.pack('>I', len(payload)) + chunk + struct.pack('>I', zlib.crc32(chunk) & 0xffffffff))
        with self.assertRaises(ValueError): check.check_png(image[:-12] + injected + image[-12:])

    def test_unreviewed_tracked_file_is_flagged_without_reading_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); (root / '.git').mkdir()
            fake = subprocess.CompletedProcess(['git'], 0, stdout=b'README.md\0private/unreviewed.txt\0')
            with patch.object(check, 'ROOT', root), patch.object(check.subprocess, 'run', return_value=fake):
                self.assertEqual(check.unreviewed_tracked_files(), ['private/unreviewed.txt'])
                self.assertFalse((root / 'private').exists())

    def test_remote_guide_shell_examples_parse_without_executing_them(self):
        guide = (ROOT / 'docs/remote-owner-setup.md').read_text(encoding='utf-8')
        blocks = re.findall(r'```bash\n(.*?)\n```', guide, re.DOTALL)
        self.assertGreaterEqual(len(blocks), 3)
        for block in blocks:
            result = subprocess.run(['/bin/bash', '-n'], input=block.encode(),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
            self.assertEqual(result.returncode, 0, 'Shell example did not parse')


if __name__ == '__main__': unittest.main()
