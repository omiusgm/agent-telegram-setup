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
    "guard_server.py", "verify_guard.py", "tests/test_conversation_guard.py", "tests/fake_guard_upstream.py",
    "telegram_setup/conversation_guard/__init__.py", "telegram_setup/conversation_guard/__main__.py",
    "telegram_setup/conversation_guard/policy.py", "telegram_setup/conversation_guard/store.py",
    "telegram_setup/conversation_guard/broker.py", "telegram_setup/conversation_guard/server.py",
    "docs/conversation-guard.md", "docs/prompt-injection-research.md",
    "examples/guard-templates.json", "examples/guarded-mcp-config.toml",
    "telegram_setup/conversation_guard/reader.py", "telegram_setup/conversation_guard/errors.py",
    "tests/guard_fixtures.py", "tests/verify_read_draft.py", "tests/test_reader_boundary.py",
    "telegram_setup/relay.py", "telegram_setup/shared.py", "telegram_setup/client.py",
    "telegram_setup/history_pages.py", "telegram_setup/owned_inventory.py",
    "tests/test_shared.py", "tests/offline_shared.py", "tests/test_read_extensions.py",
    "docs/shared-connection.md", "docs/history-and-inventory.md", "docs/notes-migration.md",
    "CHANGELOG.md", "mcp_client.py",
    "docs/linux-multi-agent-plan.md", "examples/remote-reader-codex.toml",
    "examples/remote-reader-claude.mcp.json",
    "gateway_server.py", "telegram_setup/agents.py", "telegram_setup/reader_gateway.py",
    "telegram_setup/gateway.py", "telegram_setup/dialog_pages.py",
    "tests/test_agent_gateway.py", "tests/test_gateway_http.py", "tests/test_dialog_pages.py",
    "examples/agents-readonly.json", "deploy/agent-telegram-backend.service",
    "deploy/agent-telegram-reader.service",
    "stage_linux.py", "docs/linux-service.md",
    "tests/offline_gateway.py", "tests/test_gateway_stdio.py",
    "tests/test_client_environment.py",
    "setup_web.py", "telegram_setup/web_setup.py", "telegram_setup/web_login.py",
    "telegram_setup/web_assets/index.html", "telegram_setup/web_assets/app.js",
    "telegram_setup/web_assets/style.css", "tests/test_web_setup.py",
    "TelegramSetup.app/Contents/Info.plist", "TelegramSetup.app/Contents/MacOS/run-setup",
    "scripts/create_linux_launcher.py",
    "docs/web-setup.md",
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

