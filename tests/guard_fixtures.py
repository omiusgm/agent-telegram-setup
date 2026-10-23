"""Network-free test doubles only; these are NOT runtime attestations or live adapters."""
import json
import re
from mcp import types
from telegram_setup.conversation_guard.broker import Broker, unpack
from telegram_setup.conversation_guard.errors import Denied


class FakeBackend:
    def __init__(self):
        self.messages = [{"id": 1, "text": "Привет", "sender": "Контакт", "sender_id": 12345}]
        self.calls = []
        self.fail_after_send = False

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == "get_history":
            value = {"results": list(reversed(self.messages))}
        elif name == "reply_to_message":
            self.messages.append({"id": len(self.messages)+10, "out": True,
                "text": args["text"], "reply_to": args["message_id"]})
            if self.fail_after_send:
                raise TimeoutError("synthetic_uncertain")
            value = "Replied to message 1 in chat 12345."
        else:
            raise AssertionError("Unexpected upstream action")
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(value, ensure_ascii=False))])



class FixtureWriter(Broker):
    """Exercises action-state invariants against the exact in-memory fake only."""
    def __init__(self, store, backend):
        if type(backend) is not FakeBackend:
            raise TypeError("only_exact_in_memory_fake_backend")
        super().__init__(store, backend)

    async def dispatch_once(self):
        row = self.store.claim()
        if row is None:
            return False
        action = row["payload"]
        args = {key: action[key] for key in ("chat_id", "message_id", "text", "parse_mode")}
        try:
            before = unpack(await self.upstream.call_tool("get_history", {"chat_id": action["chat_id"], "limit": 10}))
            if not isinstance(before, dict) or not isinstance(before.get("results"), list):
                raise Denied("send_baseline_unavailable")
            baseline = max((m.get("id", 0) for m in before["results"] if type(m.get("id")) is int), default=0)
            try:
                self.store.validate_before_send(row["id"])
            except Denied as exc:
                self.store.cancel_before_send(row["id"], str(exc))
                return True
            result = unpack(await self.upstream.call_tool("reply_to_message", args))
            match = re.search(r"message_id=(\d+)", result) if isinstance(result, str) else None
            recent = unpack(await self.upstream.call_tool("get_history", {"chat_id": action["chat_id"], "limit": 10}))
            messages = recent.get("results", []) if isinstance(recent, dict) else []
            matches = [m for m in messages if type(m.get("id")) is int and m["id"] > baseline
                and (match is None or m["id"] == int(match[1])) and m.get("out") is True
                and m.get("text") == action["text"] and m.get("reply_to") == action["message_id"]]
            if len(matches) != 1:
                raise Denied("send_readback_not_verified")
            self.store.finish(row["id"], message_id=matches[0]["id"])
        except Exception as exc:
            self.store.finish(row["id"], reason=str(exc) if isinstance(exc, Denied) else type(exc).__name__)
        return True

