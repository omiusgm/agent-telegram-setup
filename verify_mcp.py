"""Protocol check, offline by default; --live explicitly checks this installation."""

import argparse
import asyncio
import json
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from telegram_setup.config import ROOT, load_credentials
from telegram_setup.permissions import CAPABILITIES, PRESETS, load_settings, validate_settings


async def verify(live=False, selection=None, label="read-only"):
    with tempfile.TemporaryDirectory(prefix="telegram-mcp-test-") as temporary:
        env = dict(os.environ)
        if live:
            selection = load_settings()
            if not load_credentials().get("authorized"):
                raise RuntimeError("Сначала завершите локальный вход.")
            args = ["-m", "telegram_setup.server"]
        else:
            selection = validate_settings(selection or {"schema_version": 1, "enabled": ["read"],
                                                       "transcription_engine": None})
            env.update(AGENT_TELEGRAM_HOME=temporary)
            env.pop("AGENT_TELEGRAM_ACCESS", None)
            path = Path(temporary) / "permissions.json"
            path.write_text(json.dumps(selection), encoding="utf-8")
            path.chmod(0o600)
            args = [str(ROOT / "tests/offline_server.py")]
        parameters = StdioServerParameters(command=sys.executable, args=args, env=env, cwd=str(ROOT))
        with open(os.devnull, "w") as stderr:
            async with stdio_client(parameters, errlog=stderr) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=300)) as session:
                    await session.initialize()
                    available = (await session.list_tools()).tools
                    names = {tool.name for tool in available}
                    assert {"list_chats", "get_messages"} <= names
                    selected = set(selection["enabled"])
                    for capability, (_, _, tools) in CAPABILITIES.items():
                        for name in tools:
                            assert (name in names) == (capability in selected), (capability, name)
                    allowed_writes = {tool for capability in selected for tool in CAPABILITIES[capability][2]}
                    assert all(t.annotations and t.annotations.readOnlyHint or t.name in allowed_writes for t in available)
                    if not live and "download" not in selected:
                        rejected = await session.call_tool("download_media", {"chat_id": "me", "message_id": 1})
                        assert rejected.isError, "Unselected download_media was callable"
                    if not live and "transcribe" in selected:
                        rejected = await session.call_tool("transcribe_voice", {
                            "chat_id": "me", "message_id": 1, "engine": "groq"})
                        body = json.loads(next(c.text for c in rejected.content if c.type == "text"))
                        assert body.get("reason") == "transcription_engine_not_permitted"
                    count = None
                    if live:
                        result = await session.call_tool("list_chats", {"limit": 1})
                        if result.isError:
                            raise RuntimeError("Пробное чтение не прошло.")
                        payload = json.loads(next(c.text for c in result.content if c.type == "text"))
                        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                            raise RuntimeError("Неожиданный ответ чтения.")
                        count = len(payload["results"])
                    print(json.dumps({"mode": "live" if live else "offline", "handshake": "passed",
                                      "case": label, "capabilities": selection["enabled"], "tools": len(names),
                                      "chats_returned": count, "write_calls": 0}))


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--live", action="store_true")
    group.add_argument("--all-permissions", action="store_true", help="Offline matrix of capability choices")
    args = parser.parse_args()
    if not args.all_permissions:
        return await verify(live=args.live)
    cases = [("read-only", ["read"])]
    cases += [(name, ["read", name]) for name in CAPABILITIES if name != "read"]
    cases += [(name, value) for name, value in PRESETS.items() if name != "read-only"]
    cases += [("all-selected", list(CAPABILITIES))]
    for label, enabled in cases:
        await verify(selection={"schema_version": 1, "enabled": enabled,
                                "transcription_engine": "telegram" if "transcribe" in enabled else None}, label=label)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print("Проверка не прошла; тип ошибки: " + type(exc).__name__, file=sys.stderr)
        sys.exit(1)

