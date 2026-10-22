"""Install the pinned upstream into this repository's own environment."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from telegram_setup.config import ROOT, UPSTREAM, UPSTREAM_COMMIT, UPSTREAM_URL, check_upstream
from telegram_setup import permissions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", default="uv", help="uv executable (official Astral distribution)")
    parser.add_argument("--permissions", help="Capabilities explicitly chosen by the owner, comma-separated; include read")
    parser.add_argument("--transcription-engine", choices=permissions.ENGINES)
    args = parser.parse_args()
    if args.permissions is not None:
        selected = permissions.validate_settings({"schema_version": 1,
            "enabled": [x.strip() for x in args.permissions.split(",")],
            "transcription_engine": args.transcription_engine})
        permissions.show(selected)
    elif args.transcription_engine:
        parser.error("Выберите --permissions вместе со способом распознавания.")
    elif (permissions.private_dir() / "permissions.json").exists():
        selected = permissions.load_settings()
        print("Сохранённый набор прав сохранится. Для изменения: python -m telegram_setup.permissions")
        permissions.show(selected)
    else:
        if not sys.stdin.isatty():
            parser.error("Сначала согласуйте права; затем укажите --permissions, например read,media,download.")
        selected = permissions.choose_interactively()
        if selected is None:
            return 1
    uv = shutil.which(args.uv)
    if not uv or not shutil.which("git"):
        parser.error("Нужны git и uv. См. README.md; системный Python не изменяется.")
    if os.name != "posix":
        parser.error("Этот пример использует POSIX flock; Windows не проверен и не поддерживается.")
    if not UPSTREAM.exists():
        subprocess.run(["git", "clone", "--no-checkout", UPSTREAM_URL, str(UPSTREAM)], check=True)
        subprocess.run(["git", "-C", str(UPSTREAM), "checkout", "--detach", UPSTREAM_COMMIT], check=True)
    check_upstream()
    env = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(ROOT / ".venv"))
    command = [uv, "sync", "--project", str(UPSTREAM), "--frozen", "--no-dev", "--extra", "proxy"]
    if selected["transcription_engine"] == "whisper":
        command += ["--extra", "whisper"]
    subprocess.run(command + ["--python", "3.11"], env=env, check=True)
    permissions.save_settings(selected)
    print("Установлено в .venv. Следующий шаг: .venv/bin/python -m telegram_setup.login")


if __name__ == "__main__":
    raise SystemExit(main())

