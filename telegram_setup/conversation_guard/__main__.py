"""Trusted operator CLI. Approval/configuration are deliberately absent from MCP tools."""
import argparse
import asyncio
import json
import sys
import unicodedata
from pathlib import Path

from ..config import private_dir
from . import policy
from .server import serve
from .reader import SnapshotReader, initialize, read_json
from .errors import Denied


def preview(value):
    text = json.dumps(value, ensure_ascii=False, indent=2)
    return "".join("\\u%04x" % ord(c) if unicodedata.category(c) in {"Cf", "Zl", "Zp"}
                   or (unicodedata.category(c) == "Cc" and c not in "\n\r\t") else c for c in text)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=private_dir()/"conversation-guard")
    commands = parser.add_subparsers(dest="action", required=True)
    init = commands.add_parser("init")
    init.add_argument("--chat-id", type=int, required=True)
    init.add_argument("--hours", type=float, default=4)
    init.add_argument("--read-budget", type=int, default=240)
    init.add_argument("--snapshot-file", type=Path, help="Owner-supplied minimal JSON snapshot; no Telegram connection")
    init.add_argument("--acknowledge-risk", action="store_true")
    serve_command = commands.add_parser("serve")
    serve_command.add_argument("--require-isolation", action="store_true", help="Refuse startup until an attested runtime exists; currently always fails closed")
    commands.add_parser("status")
    commands.add_parser("revoke")
    review = commands.add_parser("review")
    review.add_argument("action_id")
    approve = commands.add_parser("approve")
    approve.add_argument("action_id")
    approve.add_argument("--sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "init":
            print(policy.WARNING)
            if not args.acknowledge_risk:
                parser.error("Сначала согласуйте режим и границу рабочего агента; --acknowledge-risk подтверждает только ознакомление, не проверку изоляции.")
            if args.snapshot_file is None:
                parser.error("Для read/draft нужен --snapshot-file; живое подключение и отправка отключены без проверенного runtime.")
            initialize(args.home, read_json(args.snapshot_file), args.chat_id, hours=args.hours, read_budget=args.read_budget)
            print("Готов локальный snapshot read/draft. Это не OS-изоляция. Подключение к Telegram, утверждение и внешняя отправка отключены.")
            return 0
        if args.action == "approve":
            raise Denied("external_writes_disabled_no_verified_runtime")
        if args.action == "serve":
            asyncio.run(serve(args.home, require_isolation=args.require_isolation))
            return 0
        reader = SnapshotReader(args.home)
        store = reader.store
        if args.action == "review":
            print(preview(store.get(args.action_id)))
        elif args.action == "revoke":
            store.revoke()
            print("Новые чтения снимка и черновики остановлены. Внешнего отправителя нет.")
        else:
            print(preview(store.summary()))
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print("Guard: " + (str(exc) if isinstance(exc, (Denied, ValueError)) else type(exc).__name__), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
