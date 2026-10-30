"""Prepare a clickable Linux launcher; no Terminal is used by the owner."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='New .desktop file; existing files are preserved')
    parser.add_argument('--reader-only', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'linux':
        parser.error('This launcher is for Linux')
    target = Path(args.output).expanduser()
    if target.exists() or target.is_symlink() or target.suffix != '.desktop':
        parser.error('Choose a new .desktop destination')
    python = ROOT / '.venv/bin/python'
    executable = str(python) if python.is_file() and os.access(python, os.X_OK) else shutil.which('python3')
    if not executable:
        parser.error('Prepare Python first')
    try:
        check = subprocess.run([executable, '-c', 'import sys; sys.exit(sys.version_info < (3, 11))'],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        if check.returncode:
            parser.error('Prepare Python 3.11 or newer before creating the launcher')
    except (OSError, subprocess.TimeoutExpired):
        parser.error('Could not check Python; prepare Python 3.11 or newer')
    # Desktop Entry quoting differs from a shell; reject rather than interpolate
    # expansion/control characters into a command or the desktop file.
    if any(any(c in value for c in '\n\r\t%"`$\\') for value in (executable, str(ROOT))):
        parser.error('Use a simple installation path for this desktop launcher')
    command = '"' + executable + '" "' + str(ROOT / 'setup_web.py') + '" --open'
    if args.reader_only:
        command += ' --reader-only'
    content = '[Desktop Entry]\nType=Application\nName=Подключить Telegram\nTerminal=false\nExec=' + command + '\n'
    with target.open('x', encoding='utf-8') as stream:
        stream.write(content)
    target.chmod(0o700)
    print('Browser launcher created. No credentials were read.')


if __name__ == '__main__':
    main()
