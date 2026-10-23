import hashlib
import json
import os
import time
import uuid
from pathlib import Path

WARNING = """Делегирование переписки: собеседник может прислать промпт-инъекцию и попытаться управлять агентом.
Conversation Guard ограничивает диалог и исходящие действия, но не изолирует сам AI-клиент.
Рабочему агенту нужен только этот MCP: без терминала, файлов, обычного Telegram MCP, других MCP и произвольных сетевых запросов.
Интерфейс не должен автоматически загружать внешние картинки и URL из ответов модели.
Если эти возможности остаются доступны, модуль можно обойти. Изоляцию хоста он автоматически не проверяет.
Текущий запуск допускает только локальный снимок и черновики: живой клиент, approval API и внешняя отправка отключены.
Проверенного OS-runtime в этом пакете пока нет. Флаг или JSON-файл не могут подтвердить изоляцию."""


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def validate(data):
    required = {"version", "instance", "chat_id", "expires_at", "send_budget", "read_limit", "read_budget",
                "draft_ttl", "templates", "upstream_command", "upstream_args", "upstream_cwd",
                "risk_acknowledged"}
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError("invalid_policy_fields")
    if type(data["version"]) is not int or data["version"] != 1:
        raise ValueError("invalid_policy_version")
    if not isinstance(data["instance"], str) or not 8 <= len(data["instance"]) <= 64:
        raise ValueError("invalid_instance")
    for key, low, high in (("chat_id", 1, 2**53-1), ("send_budget", 1, 100),
                           ("read_limit", 1, 50), ("read_budget", 1, 1440), ("draft_ttl", 30, 900)):
        if type(data[key]) is not int or not low <= data[key] <= high:
            raise ValueError("invalid_policy_" + key)
    if type(data["expires_at"]) not in (int, float) or not 0 < data["expires_at"] < 10**11:
        raise ValueError("invalid_expiry")
    if data["risk_acknowledged"] is not True:
        raise ValueError("risk_not_acknowledged")
    if not isinstance(data["templates"], dict) or len(data["templates"]) > 10:
        raise ValueError("invalid_templates")
    for name, body in data["templates"].items():
        if not isinstance(name, str) or not name.isascii() or not name.replace("_", "").isalnum() or len(name) > 40:
            raise ValueError("invalid_template_id")
        validate_text(body)
    for key in ("upstream_command", "upstream_cwd"):
        if not isinstance(data[key], str) or not Path(data[key]).is_absolute() or "\x00" in data[key]:
            raise ValueError("invalid_upstream_path")
    if (not isinstance(data["upstream_args"], list) or len(data["upstream_args"]) > 32
            or not all(isinstance(x, str) and "\x00" not in x for x in data["upstream_args"])):
        raise ValueError("invalid_upstream_args")
    return data


def validate_text(text):
    if (not isinstance(text, str) or not text.strip() or "\x00" in text
            or len(text.encode("utf-16-le")) // 2 > 3500):
        raise ValueError("invalid_reply_text")
    return text


def create(chat_id, command, args, cwd, *, hours=4, budget=10, read_budget=240, templates=None, now=None):
    if type(hours) not in (int, float) or not 0 < hours <= 24:
        raise ValueError("hours_must_be_between_zero_and_24")
    return validate({"version": 1, "instance": uuid.uuid4().hex, "chat_id": chat_id,
        "expires_at": (time.time() if now is None else now) + hours * 3600,
        "send_budget": budget, "read_limit": 30, "read_budget": read_budget, "draft_ttl": 900,
        "templates": templates if templates is not None else {}, "upstream_command": str(command), "upstream_args": list(args),
        "upstream_cwd": str(cwd), "risk_acknowledged": True})


def save(home, data):
    validate(data)
    home = Path(home)
    os.umask(0o077)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    target = home / "policy.json"
    with target.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
    target.chmod(0o600)


def load(home):
    return validate(json.loads((Path(home) / "policy.json").read_text(encoding="utf-8")))
