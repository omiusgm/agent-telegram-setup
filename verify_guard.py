"""Exercise the guarded MCP and owner CLI against a network-disabled fake backend."""
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

from telegram_setup.config import ROOT
from telegram_setup.conversation_guard import policy
from telegram_setup.conversation_guard.broker import unpack
from telegram_setup.conversation_guard.store import Store


async def verify():
    with tempfile.TemporaryDirectory(prefix="conversation-guard-offline-") as tmp:
        home = Path(tmp)/"guard"
        sink = Path(tmp)/"fake-calls.jsonl"
        config = policy.create(12345, sys.executable, [str(ROOT/"tests/fake_guard_upstream.py"), str(sink)], ROOT,
                               budget=2, templates={"ack": "Сообщение получено, вернусь с ответом."})
        policy.save(home, config)
        operator = Store(home, config)
        params = StdioServerParameters(command=sys.executable,
            args=["-m", "telegram_setup.conversation_guard", "--home", str(home), "serve"], cwd=str(ROOT), env=dict(os.environ))
        with open(os.devnull, "w") as stderr:
            async with stdio_client(params, errlog=stderr) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as worker:
                    await worker.initialize()
                    tools = {t.name for t in (await worker.list_tools()).tools}
                    assert tools == {"guard_status", "guard_read", "guard_propose_reply", "guard_get_draft", "guard_request_template"}
                    data = unpack(await worker.call_tool("guard_read", {}))
                    assert len(data["messages"]) == 4
                    assert all(m["trust"] == "untrusted" and m["source_chat_id"] == 12345 for m in data["messages"])
                    assert any(m["injection_hints"] == [] for m in data["messages"])
                    attacks = [
                        ("guard_approve", {"approved": True}),
                        ("send_file", {"chat_id": 54321, "file_path": "synthetic-private-file"}),
                        ("guard_read", {"chat_id": 54321}),
                        ("guard_read", {"account": "other"}),
                        ("guard_propose_reply", {"in_reply_to": 1, "text": "Leak", "approved": True}),
                        ("guard_propose_reply", {"in_reply_to": 999, "text": "Unobserved target"}),
                        ("guard_request_template", {"in_reply_to": 2, "template_id": "ack", "text": "attacker override"}),
                        ("guard_request_template", {"in_reply_to": 2, "template_id": "not-approved"}),
                        ("exec_command", {"cmd": "synthetic command"}),
                        ("guard_change_policy", {"chat_id": 54321}),
                    ]
                    for name, args in attacks:
                        rejected = await worker.call_tool(name, args)
                        assert rejected.isError, (name, args)
                    draft = unpack(await worker.call_tool("guard_propose_reply", {"in_reply_to": 1, "text": "Спасибо, уточню и отвечу."}))
                    assert draft["status"] == "pending"
                    calls = [json.loads(line) for line in sink.read_text().splitlines()]
                    assert not any(c["tool"] != "get_history" for c in calls)
                    row = operator.get(draft["action_id"])
                    process = await asyncio.create_subprocess_exec(sys.executable, "-m", "telegram_setup.conversation_guard",
                        "--home", str(home), "approve", row["id"], "--sha256", row["hash"], cwd=ROOT,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    await process.communicate()
                    assert process.returncode == 0

                    async def wait_sent(ident):
                        for _ in range(50):
                            current = unpack(await worker.call_tool("guard_get_draft", {"action_id": ident}))
                            if current["status"] == "sent":
                                return current
                            if current["status"] == "uncertain_do_not_retry":
                                raise AssertionError(current["reason"])
                            await asyncio.sleep(0.05)
                        raise AssertionError("Synthetic publisher did not finish")

                    await wait_sent(row["id"])
                    auto = unpack(await worker.call_tool("guard_request_template", {"in_reply_to": 2, "template_id": "ack"}))
                    await wait_sent(auto["action_id"])
                    status = unpack(await worker.call_tool("guard_status", {}))
                    assert status["attempted"] == status["budget"] == 2
                    assert status["host_isolation_verified"] is False
                    operator.revoke()
                    assert (await worker.call_tool("guard_read", {})).isError
                    assert (await worker.call_tool("guard_request_template", {"in_reply_to": 3, "template_id": "ack"})).isError
                    calls = [json.loads(line) for line in sink.read_text().splitlines()]
                    writes = [c for c in calls if c["tool"] == "reply_to_message"]
                    assert len(writes) == 2
                    assert all(c["args"]["chat_id"] == 12345 for c in calls)
                    assert writes[1]["args"]["text"] == config["templates"]["ack"]
                    assert not any(c["tool"] == "send_file" for c in calls)
                    assert not (home/"credentials.json").exists()
        return {"mode": "synthetic_offline", "mcp_initialize": "passed", "guarded_tools": len(tools),
                "unauthorized_call_cases_rejected": len(attacks), "unapproved_writes": 0,
                "approved_free_text_replies": 1, "preapproved_static_replies": 1,
                "wrong_chat_calls": 0, "raw_file_send_calls": 0, "revocation_tested": True,
                "server_readback_verified": True, "real_account_calls": 0,
                "host_sandbox_tested": False, "llm_injection_detection_benchmark": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = asyncio.run(verify())
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        args.report.chmod(0o600)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
