"""Check the explicit publication set; never inspect local account data."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FILES = (
    ".gitignore", "README.md", "AGENTS.md", "LICENSE", "bootstrap.py", "server.py", "verify_mcp.py",
    "telegram_setup/__init__.py", "telegram_setup/config.py", "telegram_setup/login.py", "telegram_setup/server.py",
    "telegram_setup/permissions.py", "telegram_setup/access_guard.py", "tests/test_permissions.py",
    "tests/test_setup.py", "tests/offline_server.py", "scripts/check_public.py",
    "docs/agent-handoff.md", "docs/troubleshooting.md", "docs/sending.md", "docs/verification.md", "docs/permissions.md",
    "examples/global-AGENTS.md", "examples/mcp-config.toml",
)
FORBIDDEN = (
    re.compile(r"/(?:Users|home)/[^\s/]+/"),
    re.compile(r"\b[0-9a-fA-F]{32}\b"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"\b(?:ghp_|github_pat_|sk-)[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\+\d[\d ()-]{9,}\d"),
)


def main():
    failures = []
    for relative in PUBLIC_FILES:
        path = ROOT / relative
        if not path.is_file() or path.is_symlink():
            failures.append(relative + ": missing or symlink")
            continue
        text = path.read_text(encoding="utf-8")
        if any(pattern.search(text) for pattern in FORBIDDEN):
            failures.append(relative + ": possible private value (content not printed)")
        if path.suffix == ".md":
            for target in re.findall(r"\]\(([^\s)]+)\)", text):
                if "://" in target or target.startswith("#"):
                    continue
                if not (path.parent / target.split("#", 1)[0]).resolve().is_file():
                    failures.append(relative + ": broken relative link")
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"Public check passed: {len(PUBLIC_FILES)} explicit files, no flagged values or broken local links")


if __name__ == "__main__":
    main()

