"""Use the existing shared Telegram MCP from any local agent working directory.

This client never loads Telegram credentials or opens the account session.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]

# The MCP SDK deliberately inherits only a small OS environment by default.
# Carry the selected installation/route to the relay, never a parent session.
RELAY_ENV_KEYS = (
    'AGENT_TELEGRAM_HOME', 'AGENT_TELEGRAM_ACCESS',
    'AGENT_TELEGRAM_MANAGED_BACKEND',
    'AGENT_TELEGRAM_SOCKS_HOST', 'AGENT_TELEGRAM_SOCKS_PORT',
    'TELEGRAM_TRANSCRIBE_OPENAI_URL', 'TELEGRAM_TRANSCRIBE_OPENAI_API_KEY',
    'TELEGRAM_TRANSCRIBE_OPENAI_MODEL', 'TELEGRAM_TRANSCRIBE_OPENAI_MAX_MB',
    'TELEGRAM_TRANSCRIBE_WHISPER_MODEL', 'TELEGRAM_TRANSCRIBE_WHISPER_DEVICE',
    'TELEGRAM_TRANSCRIBE_WHISPER_COMPUTE_TYPE', 'TELEGRAM_TRANSCRIBE_WHISPER_MODEL_DIR',
)


def relay_parameters():
    return StdioServerParameters(command=sys.executable,
        args=[str(ROOT / 'server.py')], cwd=str(ROOT),
        env={name: os.environ[name] for name in RELAY_ENV_KEYS if name in os.environ})


def text_payload(result):
    texts = [c.text for c in result.content if c.type == "text"]
    if len(texts) == 1:
        try:
            return json.loads(texts[0])
        except json.JSONDecodeError:
            return texts[0]
    return texts


async def run(args):
    parameters = relay_parameters()
    with open(os.devnull, "w") as stderr:
        async with stdio_client(parameters, errlog=stderr) as (reader, writer):
            async with ClientSession(reader, writer,
                    read_timeout_seconds=timedelta(seconds=args.timeout)) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                names = {t.name for t in tools}
                if args.action == "tools":
                    result = [{"name": t.name, "description": t.description,
                               "inputSchema": t.inputSchema,
                               "annotations": t.annotations.model_dump() if t.annotations else None}
                              for t in tools if not args.name or t.name == args.name]
                elif args.action == "status":
                    if not {"get_me", "list_chats", "get_messages"} <= names:
                        raise RuntimeError("Telegram read tools missing from MCP catalog")
                    probe = await session.call_tool("get_me", {})
                    payload = text_payload(probe)
                    if (probe.isError or not isinstance(payload, dict)
                            or payload.get("error") or payload.get("type") != "user"
                            or type(payload.get("id")) is not int or payload["id"] <= 0):
                        raise RuntimeError("Telegram read-only connection probe failed")
                    result = {"mcp_initialized": True, "connection_probe": "passed",
                              "tools_available": len(names), "send_file": "send_file" in names,
                              "download_media": "download_media" in names,
                              "read_history_page": "read_history_page" in names,
                              "read_owned_inventory": "read_owned_inventory" in names,
                              "write_calls": 0}
                else:
                    if args.name not in names:
                        raise RuntimeError("Tool is not exposed by the existing MCP")
                    payload = json.loads(Path(args.args_file).read_text()) if args.args_file else json.loads(args.args)
                    if not isinstance(payload, dict):
                        raise ValueError("Tool arguments must be a JSON object")
                    reply = await session.call_tool(args.name, payload)
                    result = {"isError": bool(reply.isError), "result": text_payload(reply)}
                print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "tools", "call"])
    parser.add_argument("name", nargs="?")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--args", default="{}", help="JSON object with operational tool parameters; no login secrets")
    group.add_argument("--args-file", help="Local UTF-8 JSON file with operational tool parameters")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    if args.action == "call" and not args.name:
        parser.error("call requires a tool name")
    if not 1 <= args.timeout <= 3600:
        parser.error("timeout must be between 1 and 3600 seconds")
    try:
        asyncio.run(run(args))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print("MCP request failed: " + type(exc).__name__ +
              ". Check the existing service; do not repeat an uncertain send.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
