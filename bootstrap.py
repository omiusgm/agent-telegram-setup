"""Install the pinned upstream into this repository's own environment."""

import argparse
import os
import shutil
import subprocess
from pathlib import Path

from telegram_setup.config import ROOT, UPSTREAM, UPSTREAM_COMMIT, UPSTREAM_URL, check_upstream


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", default="uv", help="uv executable (official Astral distribution)")
    args = parser.parse_args()
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
    subprocess.run([uv, "sync", "--project", str(UPSTREAM), "--frozen", "--no-dev",
                    "--extra", "proxy", "--python", "3.11"], env=env, check=True)
    print("Установлено в .venv. Следующий шаг: .venv/bin/python -m telegram_setup.login")


if __name__ == "__main__":
    main()

