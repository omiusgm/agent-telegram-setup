"""Trusted operator CLI. Approval/configuration are deliberately absent from MCP tools."""
import argparse
import asyncio
import json
import sys
import unicodedata
from pathlib import Path

from ..config import ROOT, private_dir
from . import policy
from .server import serve
from .store import Denied, Store


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
    init.add_argument("--budget", type=int, default=10)
    init.add_argument("--read-budget", type=int, default=240)
    init.add_argument("--templates-file", type=Path)
    init.add_argument("--upstream-command", default=str(ROOT/".venv/bin/python"))
    init.add_argument("--upstream-arg", action="append", default=None)
    init.add_argument("--upstream-cwd", default=str(ROOT))
    init.add_argument("--acknowledge-risk", action="store_true")
    commands.add_parser("serve")
    commands.add_parser("status")
    commands.add_parser("revoke")
    review = commands.add_parser("review")
    review.add_argument("action_id")
    approve = commands.add_parser("approve")
    approve.add_argument("action_id")
    approve.add_argument("--sha256", required=True)
    reject = commands.add_parser("reject")
    reject.add_argument("action_id")
    args = parser.parse_args(argv)
    try:
        if args.action == "init":
            print(policy.WARNING)
            if not args.acknowledge_risk:
                parser.error("Сначала согласуйте режим и границу рабочего агента; --acknowledge-risk подтверждает только ознакомление, не проверку изоляции.")
            templates = json.loads(args.templates_file.read_text()) if args.templates_file else {}
            data = policy.create(args.chat_id, args.upstream_command, args.upstream_arg if args.upstream_arg is not None else [str(ROOT/"server.py")],
                                 args.upstream_cwd, hours=args.hours, budget=args.budget, read_budget=args.read_budget, templates=templates)
            policy.save(args.home, data)
            Store(args.home, data)
            print("Готов тестовый режим одного диалога. Рабочему агенту подключите только этот MCP; владелец рассматривает черновики отдельно.")
            return 0
        data = policy.load(args.home)
        if args.action == "serve":
            asyncio.run(serve(args.home))
            return 0
        store = Store(args.home, data)
        if args.action == "review":
            print(preview(store.get(args.action_id)))
        elif args.action == "approve":
            store.approve(args.action_id, args.sha256)
            print("Утверждено конкретное действие; это не подтверждение доставки. Проверьте его состояние после отправки.")
        elif args.action == "revoke":
            store.revoke()
            print("Новые действия делегирования остановлены. Уже начавшаяся отправка может завершиться.")
        elif args.action == "reject":
            store.reject(args.action_id)
            print("Неотправленный черновик отклонён.")
        else:
            print(preview(store.summary()))
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print("Guard: " + (str(exc) if isinstance(exc, (Denied, ValueError)) else type(exc).__name__), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
