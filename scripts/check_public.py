"""Check the explicit publication set; never inspect local account data."""

import re
import json
import struct
import xml.etree.ElementTree as ET
import zlib
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
    "docs/knowledge-catalog.md", "examples/knowledge-catalog.schema.json",
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
    "telegram_setup/saved_text.py", "tests/test_saved_text.py", "tests/offline_saved_text.py",
    "docs/saved-text-mvp.md",
    "docs/dot-client-guide.md", "docs/dot-agent-handoff.md", "docs/setup-flow-contract.md",
    "docs/dot-guide-steps.json", "scripts/build_dot_guide.py", "scripts/render_dot_guide.cjs",
    "docs/assets/dot-guide/00-route.svg", "docs/assets/dot-guide/00-route.png",
    "docs/assets/dot-guide/01-telegram.svg", "docs/assets/dot-guide/01-telegram.png",
    "docs/assets/dot-guide/02-tunnel.svg", "docs/assets/dot-guide/02-tunnel.png",
    "docs/assets/dot-guide/03-runtime-key.svg", "docs/assets/dot-guide/03-runtime-key.png",
    "docs/assets/dot-guide/04-polling.svg", "docs/assets/dot-guide/04-polling.png",
    "docs/assets/dot-guide/05-plugin.svg", "docs/assets/dot-guide/05-plugin.png",
    "docs/assets/dot-guide/06-dot-read.svg", "docs/assets/dot-guide/06-dot-read.png",
    "docs/assets/dot-guide/07-saved-text.svg", "docs/assets/dot-guide/07-saved-text.png",
    "docs/saved-files-and-search-plan.md", "tests/offline_file_transport.py",
    "tests/test_file_transport_contract.py",
    "telegram_setup/private_state.py", "telegram_setup/scoped_search.py", "telegram_setup/search_pages.py",
    "telegram_setup/file_transport.py", "telegram_setup/saved_files.py", "telegram_setup/file_gateway.py",
    "tests/test_scoped_search.py", "tests/test_search_pages.py", "tests/test_saved_files.py",
    "tests/test_search_files_stdio.py", "tests/offline_search_files.py", "docs/scoped-search-and-files.md",
    "telegram_setup/file_probe.py", "tests/test_file_probe.py",
    "telegram_setup/account_search.py", "tests/test_account_search.py",
    "telegram_setup/photos.py", "telegram_setup/photo_gateway.py", "docs/selected-photos.md",
    "tests/test_photos.py", "tests/test_photos_stdio.py", "tests/offline_photos.py",
    "telegram_setup/peer_profiles.py", "telegram_setup/peer_gateway.py", "docs/peer-profiles.md",
    "tests/test_peer_profiles.py", "tests/test_peer_stdio.py", "tests/offline_peer_profiles.py",
    "telegram_setup/attachment_gateway.py", "tests/test_attachments.py", "tests/test_attachment_stdio.py",
    "docs/attachments.md",
    'telegram_setup/source_intents.py',
    'telegram_setup/requested_sources.py',
    'tests/test_requested_sources.py',
    'docs/requested-sources.md',
    'CONTRIBUTING.md',
    'SECURITY.md',
    'CODE_OF_CONDUCT.md',
    '.github/ISSUE_TEMPLATE/bug_report.yml',
    '.github/ISSUE_TEMPLATE/feature_request.yml',
    '.github/ISSUE_TEMPLATE/config.yml',
    '.github/pull_request_template.md',
    '.github/workflows/community.yml',
)
FORBIDDEN = (
    re.compile(r"/(?:Users|home)/[^\s/]+/"),
    re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])"),
    re.compile(r"\b(?:tunnel_[A-Za-z0-9]{16,}|org-[A-Za-z0-9]{12,})\b"),
    re.compile(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"\b(?:ghp_|github_pat_|sk-)[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\+\d[\d ()-]{9,}\d"),
)


def check_png(data):
    if not data.startswith(b'\x89PNG\r\n\x1a\n') or len(data) > 3000000:
        raise ValueError('invalid PNG header or size')
    offset, kinds = 8, []
    dimensions = None
    while offset < len(data):
        if offset + 12 > len(data):
            raise ValueError('truncated PNG')
        length = struct.unpack('>I', data[offset:offset+4])[0]
        end = offset + length + 12
        kind = data[offset+4:offset+8]
        if end > len(data) or kind not in (b'IHDR', b'IDAT', b'IEND'):
            raise ValueError('PNG contains unsupported or metadata chunks')
        if zlib.crc32(data[offset+4:end-4]) & 0xffffffff != struct.unpack('>I', data[end-4:end])[0]:
            raise ValueError('PNG chunk checksum mismatch')
        if kind == b'IHDR':
            if length != 13:
                raise ValueError('invalid PNG dimensions')
            dimensions = struct.unpack('>II', data[offset+8:offset+16])
        kinds.append(kind)
        offset = end
    if kinds.count(b'IHDR') != 1 or kinds[0] != b'IHDR' or kinds[-1] != b'IEND' or b'IDAT' not in kinds:
        raise ValueError('invalid PNG structure')
    if dimensions != (1200, 800):
        raise ValueError('unexpected teaching image dimensions')
    return dimensions


def check_svg(text):
    if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper() or '@import' in text:
        raise ValueError('active SVG content')
    root = ET.fromstring(text)
    allowed = {'svg', 'title', 'desc', 'style', 'g', 'rect', 'circle', 'line', 'polyline', 'path', 'polygon', 'text', 'tspan'}
    for node in root.iter():
        if node.tag.rsplit('}', 1)[-1] not in allowed:
            raise ValueError('unsupported SVG element')
        for key, value in node.attrib.items():
            if key.lower().startswith('on') or key.rsplit('}', 1)[-1] == 'href' or 'url(' in value:
                raise ValueError('external or active SVG attribute')
    if 'url(' in text or root.get('viewBox') != '0 0 1200 800':
        raise ValueError('unexpected SVG resources or canvas')
    if 'Учебная иллюстрация' not in text or 'условные данные' not in text:
        raise ValueError('missing teaching illustration label')


def check_guide_index():
    data = json.loads((ROOT / 'docs/dot-guide-steps.json').read_text())
    steps = data.get('steps', [])
    if len(steps) != 8 or len({row['id'] for row in steps}) != 8:
        raise ValueError('invalid guide step map')
    for row in steps:
        if row.get('kind') != 'teaching_illustration' or row.get('uses_real_account_data') is not False:
            raise ValueError('illustration provenance missing')
        for key in ('image', 'editable_source', 'guide'):
            target, _, anchor = row[key].partition('#')
            path = (ROOT / 'docs' / target).resolve()
            if not path.is_relative_to((ROOT / 'docs').resolve()) or not path.is_file():
                raise ValueError('guide path missing or outside docs')
            if 'docs/' + target not in PUBLIC_FILES:
                raise ValueError('guide references a non-public file')
            if anchor and f'id="{anchor}"' not in path.read_text():
                raise ValueError('guide anchor missing')


def main():
    failures = []
    for relative in PUBLIC_FILES:
        path = ROOT / relative
        if not path.is_file() or path.is_symlink():
            failures.append(relative + ": missing or symlink")
            continue
        if path.suffix == '.png':
            try:
                check_png(path.read_bytes())
            except (ValueError, struct.error):
                failures.append(relative + ': invalid teaching PNG or embedded metadata')
            continue
        text = path.read_text(encoding="utf-8")
        if any(pattern.search(text) for pattern in FORBIDDEN):
            failures.append(relative + ": possible private value (content not printed)")
        if path.suffix == '.svg':
            try:
                check_svg(text)
            except (ValueError, ET.ParseError):
                failures.append(relative + ': invalid or active teaching SVG')
        if path.suffix == ".md":
            for target in re.findall(r"\]\(([^\s)]+)\)", text):
                if "://" in target or target.startswith("#"):
                    continue
                if not (path.parent / target.split("#", 1)[0]).resolve().is_file():
                    failures.append(relative + ": broken relative link")
    try:
        check_guide_index()
    except (OSError, ValueError, KeyError):
        failures.append('docs/dot-guide-steps.json: invalid step/image/link mapping')
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"Public check passed: {len(PUBLIC_FILES)} explicit files, no flagged values or broken local links")


if __name__ == "__main__":
    main()

