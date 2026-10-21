"""Owner runs this in a local Terminal. Never collect login secrets in chat."""

import argparse
import asyncio
import getpass
import logging
import re
import sys
import time
from datetime import datetime, timezone

import qrcode
from telethon import TelegramClient, errors

from .config import DEVICE, connection_lock, load_credentials, prepare_private, proxy_config, save_credentials


def api_credentials():
    data = load_credentials()
    if data:
        return data
    print("Получите свои API ID / API Hash на https://my.telegram.org/apps")
    raw_id = getpass.getpass("API ID (скрытый ввод): ").strip()
    api_hash = getpass.getpass("API Hash (скрытый ввод): ").strip()
    if not raw_id.isdecimal() or int(raw_id) <= 0 or not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash):
        raise ValueError("Неверный формат API ID / API Hash. Значения не отправляйте в чат.")
    data = {"api_id": int(raw_id), "api_hash": api_hash, "authorized": False}
    save_credentials(data)
    return data


async def password_login(client):
    for _ in range(3):
        password = getpass.getpass("Пароль двухэтапной защиты Telegram (скрыто): ")
        try:
            await client.sign_in(password=password)
            return
        except errors.PasswordHashInvalidError:
            print("Пароль не подошёл.")
        finally:
            password = None
    raise RuntimeError("Вход не завершён; запустите его позднее.")


def render_qr(qr, image_path=None):
    image = qrcode.QRCode(border=4, box_size=8)
    image.add_data(qr.url)
    image.make(fit=True)
    if image_path:
        image.make_image(fill_color="black", back_color="white").save(image_path, format="PNG")
        image_path.chmod(0o600)
        print("Свежий QR сохранён локально в .local/login-qr.png; не пересылайте его.")
    else:
        image.print_ascii(invert=True)


async def qr_login(client, image_path=None):
    deadline = time.monotonic() + 600
    qr = await client.qr_login()
    while time.monotonic() < deadline:
        seconds = max(1, (qr.expires - datetime.now(timezone.utc)).total_seconds())
        waiter = asyncio.create_task(qr.wait(timeout=seconds))
        # Start Telegram's update listener BEFORE making the QR visible.
        await asyncio.sleep(0)
        try:
            render_qr(qr, image_path)
            print("На телефоне: Telegram → Настройки → Устройства → Подключить устройство.")
            await waiter
            return
        except asyncio.TimeoutError:
            print("QR истёк, обновляю. Сканируйте только новый код.")
            await qr.recreate()
        except errors.SessionPasswordNeededError:
            if image_path:
                image_path.unlink(missing_ok=True)
            await password_login(client)
            return
        finally:
            if not waiter.done():
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
    raise RuntimeError("Время входа истекло. Запустите вход заново.")


async def authorize(data, phone_mode=False, image_path=None):
    client = TelegramClient(str(prepare_private() / "account"), data["api_id"], data["api_hash"],
                            device_model=DEVICE, system_version="local", app_version="0.1.0",
                            proxy=proxy_config(), timeout=15, connection_retries=2,
                            request_retries=1, flood_sleep_threshold=0)
    try:
        await asyncio.wait_for(client.connect(), timeout=45)
        if not await client.is_user_authorized():
            if phone_mode:
                phone = getpass.getpass("Телефон с кодом страны (скрыто): ").strip()
                code = None
                try:
                    await client.send_code_request(phone)
                    code = getpass.getpass("Код из Telegram (скрыто): ").replace(" ", "")
                    try:
                        await client.sign_in(phone=phone, code=code)
                    except errors.SessionPasswordNeededError:
                        await password_login(client)
                finally:
                    phone = code = None
            else:
                await qr_login(client, image_path)
        me = await client.get_me()
        if me.bot or (data.get("user_id") and data["user_id"] != me.id):
            raise RuntimeError("Нужен ожидаемый личный аккаунт. Настройки не заменены.")
        data.update(authorized=True, user_id=me.id, username=me.username)
        save_credentials(data)
        print("Вход сохранён. Устройство в Telegram: " + DEVICE)
    finally:
        await client.disconnect()
        if image_path:
            image_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phone", action="store_true", help="Phone/code instead of QR; input remains hidden")
    parser.add_argument("--qr-image", action="store_true", help="Temporary local PNG instead of terminal QR")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        print("Откройте собственный Terminal и выполните вход там. Не вводите секреты в чат.", file=sys.stderr)
        return 2
    logging.getLogger("telethon").setLevel(logging.CRITICAL)
    image = prepare_private() / "login-qr.png" if args.qr_image else None
    try:
        with connection_lock():
            asyncio.run(authorize(api_credentials(), args.phone, image))
        return 0
    except errors.FloodWaitError as exc:
        print(f"Telegram требует подождать {exc.seconds} секунд. Не повторяйте раньше.", file=sys.stderr)
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
    except (KeyboardInterrupt, EOFError):
        print("Вход остановлен.", file=sys.stderr)
    except Exception as exc:
        print("Вход не завершён; тип ошибки: " + type(exc).__name__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

