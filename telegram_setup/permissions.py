"""Choose and persist MCP capabilities without touching Telegram authorization."""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from .config import PROFILES, prepare_private, private_dir


# One choice may cover several formats served by the same Telegram tool.
CAPABILITIES = {
    "read": ("Чтение переписки и контактов", "Диалоги, сообщения, поиск и метаданные; базовая возможность.", ()),
    "media": ("Просмотр вложений", "Читать документы и показывать фотографии в контексте агента.",
              ("inspect_document", "open_photo", "get_photo_sheet", "get_user_photos")),
    "download": ("Скачивание вложений на компьютер", "Сохранять документы, картинки, аудио и видео в папку установки.",
                 ("download_media", "preview_custom_emoji")),
    "transcribe": ("Расшифровка голосовых и видеокружков", "Получать текст; способ распознавания выбирается отдельно.",
                   ("transcribe_voice",)),
    "send-text": ("Отправка сообщений и ответов", "Писать в согласованные пользователем чаты.",
                  ("send_message", "reply_to_message")),
    "send-files": ("Загрузка и отправка файлов", "Документы, картинки, альбомы, аудио, видео, стикеры и GIF.",
                   ("send_file", "send_album", "send_voice", "upload_file", "send_sticker", "send_gif")),
    "saved-files": ("Scoped-файлы в Избранном", "Только временные файлы с ACL агента и проверкой скачиванием обратно.",
                    ("send_staged_saved_file", "download_saved_attachment")),
    "forward": ("Пересылка сообщений", "Пересылать выбранные сообщения в согласованный чат.",
                ("forward_message", "forward_messages")),
    "edit": ("Редактирование сообщений и черновиков", "Менять доступные аккаунту сообщения, сохранять и очищать черновики.",
             ("edit_message", "save_draft", "clear_draft")),
    "delete": ("Удаление сообщений и истории", "Включая пакетное удаление и очистку истории; возможна потеря данных.",
               ("delete_message", "delete_messages_bulk", "delete_chat_history", "delete_scheduled_message")),
    "organize": ("Порядок в чатах", "Прочитанность, архив, уведомления, закрепления и папки.",
                 ("mark_as_read", "archive_chat", "unarchive_chat", "mute_chat", "unmute_chat",
                  "pin_message", "unpin_message", "unpin_all_messages", "update_folder", "create_folder",
                  "add_chat_to_folder", "remove_chat_from_folder", "delete_folder", "reorder_folders")),
    "read-photo": ("Выбранные фото для удалённых агентов", "Только точные chat_id/message_id, отдельно разрешённые каждому агенту.",
                   ("download_scoped_photo",)),
    "agent-profiles": ("Карточки и аватарки для удалённых агентов", "Чтение профилей и доступных аватарок в разрешённой области агента, без изменения профиля.",
                       ("read_agent_peer_profile", "list_agent_avatars", "download_agent_avatar")),
    "agent-attachments": ("Вложения для удалённых агентов", "Байты выбранного сообщения в разрешённом чате: документы и фото, без отправок.",
                          ("download_agent_attachment",)),
    "requested-source-join": ("Вступление в источники по запросу владельца", "Только ссылка, полученная из подтверждённого сообщения владельца агенту; без отправки сообщений.",
                              ("join_owner_requested_source",)),
    "agent-poll-read": ("Результаты опросов для удалённых агентов", "Читать состояние и доступные результаты в разрешённых чатах; без голосования.",
                        ("agent_read_poll",)),
    "agent-poll-create": ("Создание опросов для удалённых агентов", "Отдельное адресное разрешение и устойчивый request_id обязательны.",
                          ("agent_create_poll",)),
    "agent-poll-close": ("Закрытие своих опросов для удалённых агентов", "Только опрос, авторство которого Telegram подтверждает для текущего аккаунта.",
                         ("agent_close_own_poll",)),
    "agent-bot-read": ("Список и профили своих ботов", "Только боты владельца, без токенов и переписки BotFather.",
                       ("agent_list_owned_bots", "agent_get_owned_bot_profile")),
    "agent-bot-edit": ("Изменение профилей своих ботов", "Отдельные ID или явно разрешённые все свои боты; без токена и изменения настроек.",
                       ("agent_update_owned_bot_profile",)),
    "agent-bot-create": ("Создание ботов", "Отдельное право агента; режим оператора по умолчанию или явно выбранное делегирование.",
                         ("agent_request_owned_bot",)),
    "agent-bot-notify": ("Уведомления владельцу от выбранного бота", "Только личный чат владельца и отдельно разрешённый бот, без входящих команд.",
                         ("agent_notify_owner_via_bot",)),
}
ENGINES = {
    "telegram": "Telegram: распознавание средствами Telegram; доступность и лимиты зависят от аккаунта.",
    "whisper": "Локальный faster-whisper: аудио обрабатывается на компьютере; нужны модель и дополнительные зависимости.",
    "groq": "Groq: аудио передаётся сервису Groq; нужны свой ключ и доступная квота, возможна оплата.",
    "openai": "OpenAI-совместимый endpoint: аудио передаётся на настроенный адрес; нужен отдельный выбор сервиса/URL и при необходимости ключ.",
}
LEGACY = {"read-only": ["read"], "send-text": ["read", "send-text"],
          "send-files": ["read", "send-text", "send-files"]}
PRESETS = {"read-only": ["read"], "reader": ["read", "media", "download"],
           "assistant": ["read", "media", "download", "send-text", "send-files"]}


def validate_settings(data):
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("Неподдерживаемый формат разрешений; запустите telegram_setup.permissions.")
    enabled = data.get("enabled")
    if (not isinstance(enabled, list) or not all(isinstance(x, str) for x in enabled)
            or len(enabled) != len(set(enabled)) or not set(enabled) <= CAPABILITIES.keys()
            or "read" not in enabled):
        raise ValueError("Неизвестный набор разрешений; проверьте выбранные возможности.")
    engine = data.get("transcription_engine")
    if ("transcribe" in enabled and engine not in ENGINES) or ("transcribe" not in enabled and engine is not None):
        raise ValueError("Для расшифровки выберите один способ распознавания.")
    return {"schema_version": 1, "enabled": [x for x in CAPABILITIES if x in enabled],
            "transcription_engine": engine}


def load_settings():
    legacy = os.environ.get("AGENT_TELEGRAM_ACCESS")
    if legacy is not None and legacy not in PROFILES:
        raise ValueError("Unknown AGENT_TELEGRAM_ACCESS profile")
    path = private_dir() / "permissions.json"
    if path.exists():
        # A saved granular choice supersedes the old three-profile setting.
        return validate_settings(json.loads(path.read_text(encoding="utf-8")))
    if legacy is not None:
        return validate_settings({"schema_version": 1, "enabled": LEGACY[legacy], "transcription_engine": None})
    raise RuntimeError("Сначала выберите права: python -m telegram_setup.permissions")


def save_settings(data):
    data = validate_settings(data)
    folder = prepare_private()
    fd, temporary = tempfile.mkstemp(prefix=".permissions-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, folder / "permissions.json")
        (folder / "permissions.json").chmod(0o600)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return data


def exposed_tools(data):
    enabled = set(validate_settings(data)["enabled"])
    extras = {tool for name in enabled for tool in CAPABILITIES[name][2]}
    optional_reads = {"media": CAPABILITIES["media"][2], "transcribe": ("transcribe_voice",),
                      "download": ("preview_custom_emoji",),
                      "saved-files": ("download_saved_attachment",),
                      "read-photo": ("download_scoped_photo",),
                      "agent-profiles": CAPABILITIES["agent-profiles"][2],
                      "agent-attachments": CAPABILITIES["agent-attachments"][2],
                      "agent-poll-read": CAPABILITIES["agent-poll-read"][2],
                      "agent-bot-read": CAPABILITIES["agent-bot-read"][2]}
    excluded = {tool for name, tools in optional_reads.items() if name not in enabled for tool in tools}
    return "read-only" + ("+" + ",".join(sorted(extras)) if extras else "") + ("-" + ",".join(sorted(excluded)) if excluded else "")


def show(data):
    for name, (title, detail, _) in CAPABILITIES.items():
        print(f"[{'x' if name in data['enabled'] else ' '}] {title} ({name})")
    if data["transcription_engine"]:
        print("Распознавание: " + ENGINES[data["transcription_engine"]])
    if set(data["enabled"]) & {"send-text", "send-files", "forward", "edit", "delete"}:
        print("Если агент будет самостоятельно вести диалоги: есть риск промпт-инъекций из сообщений собеседника. Перед делегированием выберите отдельный ограниченный режим; см. docs/conversation-guard.md. Разовые поручения работают в выбранных правах.")


def choose_interactively(input_fn=None):
    ask = input_fn or input
    print("Какие возможности дать агенту в Telegram? Чтение — базовая возможность.")
    optional = [name for name in CAPABILITIES if name != "read"]
    for number, name in enumerate(optional, 1):
        title, detail, _ = CAPABILITIES[name]
        print(f"{number}. {title}. {detail}")
    print("Введите номера через запятую; 0 — только чтение. Пустой ввод ничего не выбирает.")
    while True:
        raw = ask("Ваш выбор: ").strip()
        parts = [x.strip() for x in raw.split(",")]
        if raw == "0":
            enabled = ["read"]
            break
        if raw and all(x.isdecimal() and 1 <= int(x) <= len(optional) for x in parts):
            enabled = ["read"] + [optional[i] for i in sorted({int(x) - 1 for x in parts})]
            break
        print("Укажите номера из списка или 0; настройки пока не изменены.")
    engine = None
    if "transcribe" in enabled:
        print("Выберите способ распознавания. Аудио не отправляется внешнему провайдеру без этого выбора.")
        for number, (name, detail) in enumerate(ENGINES.items(), 1):
            print(f"{number}. {detail} ({name})")
        while True:
            raw = ask("Способ распознавания: ").strip()
            if raw.isdecimal() and 1 <= int(raw) <= len(ENGINES):
                engine = list(ENGINES)[int(raw) - 1]
                break
    data = validate_settings({"schema_version": 1, "enabled": enabled, "transcription_engine": engine})
    show(data)
    if ask("Сохранить этот набор прав? [да/нет]: ").strip().lower() not in {"да", "yes", "y"}:
        return None
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--allow", help="Explicit comma-separated capabilities; include read")
    choice.add_argument("--preset", choices=PRESETS)
    choice.add_argument("--show", action="store_true", help="Show effective selection without opening credentials")
    parser.add_argument("--transcription-engine", choices=ENGINES)
    parser.add_argument("--yes", action="store_true", help="Save the explicitly supplied selection without a Terminal prompt")
    args = parser.parse_args(argv)
    try:
        if args.show:
            data = load_settings()
            print(json.dumps({**data, "tools_filter": exposed_tools(data)}, ensure_ascii=False, indent=2))
            return 0
        if args.allow is not None or args.preset:
            enabled = [x.strip() for x in args.allow.split(",")] if args.allow is not None else PRESETS[args.preset]
            data = validate_settings({"schema_version": 1, "enabled": enabled, "transcription_engine": args.transcription_engine})
            show(data)
            if not args.yes:
                if not sys.stdin.isatty():
                    parser.error("Для сохранения явного выбора без Terminal добавьте --yes.")
                if input("Сохранить этот набор прав? [да/нет]: ").strip().lower() not in {"да", "yes", "y"}:
                    return 1
        else:
            if args.transcription_engine or args.yes:
                parser.error("Сначала укажите --allow или --preset.")
            if not sys.stdin.isatty():
                parser.error("Выбор прав требует Terminal или явных --allow/--preset и --yes.")
            data = choose_interactively()
            if data is None:
                print("Настройки не изменены.")
                return 1
        save_settings(data)
        print("Права сохранены локально. Для применения перезапустите MCP; новый вход в Telegram не нужен.")
        return 0
    except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nВыбор отменён; настройки не изменены.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
