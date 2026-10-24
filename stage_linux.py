"""Install disabled Linux user units. No login, session copying, or listeners."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from telegram_setup.config import ROOT, prepare_private


def main():
    if sys.platform != 'linux':
        raise RuntimeError('This staging command is for Linux only')
    if ROOT.resolve() != (Path.home() / 'services/agent-telegram-mcp').resolve():
        raise RuntimeError('Install source under ~/services/agent-telegram-mcp first')
    folder = prepare_private()
    registry = folder / 'agents.json'
    if not registry.exists():
        from telegram_setup.agents import AgentRegistry
        AgentRegistry(registry).save({'schema_version': 1, 'agents': {}})
    units = Path.home() / '.config/systemd/user'
    units.mkdir(parents=True, exist_ok=True)
    selected = ('agent-telegram-backend.service', 'agent-telegram-reader.service')
    for name in selected:
        source, target = ROOT / 'deploy' / name, units / name
        if target.is_symlink() or (target.exists() and target.read_bytes() != source.read_bytes()):
            raise RuntimeError('Existing unit differs; review it before updating')
    for name in selected:
        target = units / name
        if not target.exists():
            shutil.copyfile(ROOT / 'deploy' / name, target)
            target.chmod(0o600)
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    print(json.dumps({'units_installed': list(selected), 'enable_requested': False,
                      'start_requested': False, 'private_directory': str(folder)}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Linux staging failed: ' + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
