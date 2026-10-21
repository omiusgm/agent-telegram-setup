"""Protocol check, offline by default; --live explicitly checks this installation."""

import argparse
import asyncio
import json
import os
import sys
import tempfile
from datetime import timedelta

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from telegram_setup.config import ROOT, access_profile, load_credentials


async def verify(live=False):
    with tempfile.TemporaryDirectory(prefix="telegram-mcp-test-") as temporary:
        env = dict(os.environ)
        if live:
            if not load_credentials().get("authorized"):
                raise RuntimeError("Сначала завершите локальный вход.")
            args = ["-m", "telegram_setup.server"]
        else:
            env.update(AGENT_TELEGRAM_HOME=temporary)
            args = [str(ROOT / "tests/offline_server.py")]
        parameters = StdioServerParameters(command=sys.executable, args=args, env=env, cwd=str(ROOT))
        with open(os.devnull, "w") as stderr:
            async with stdio_client(parameters, errlog=stderr) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=90)) as session:
                    await session.initialize()
                    available = (await session.list_tools()).tools
                    names = {tool.name for tool in available}
                    assert {"list_chats", "get_messages"} <= names
                    if access_profile() == "read-only":
                        assert all(t.annotations and t.annotations.readOnlyHint for t in available)
                        assert "send_message" not in names and "send_file" not in names
                    else:
                        assert {"send_message", "reply_to_message"} <= names
                        assert ("send_file" in names) == (access_profile() == "send-files")
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
                                      "profile": access_profile(), "tools": len(names),
                                      "chats_returned": count, "write_calls": 0}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    try:
        asyncio.run(verify(parser.parse_args().live))
    except Exception as exc:
        print("Проверка не прошла; тип ошибки: " + type(exc).__name__, file=sys.stderr)
        sys.exit(1)

