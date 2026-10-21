"""Configuration stays local; never log credentials or session contents."""

import fcntl
import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / ".upstream"
UPSTREAM_URL = "https://github.com/chigwell/telegram-mcp.git"
UPSTREAM_COMMIT = "e55d9c7a66a8eb0855debb268a4ed5b63749af2d"
DEVICE = "Personal Agent MCP"
PROFILES = {
    "read-only": "read-only",
    "send-text": "read-only+send_message,reply_to_message",
    "send-files": "read-only+send_message,reply_to_message,send_file",
}


def private_dir():
    return Path(os.environ.get("AGENT_TELEGRAM_HOME", str(ROOT / ".local"))).expanduser().resolve()


def prepare_private():
    os.umask(0o077)
    path = private_dir()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def load_credentials():
    path = private_dir() / "credentials.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or type(data.get("api_id")) is not int
            or data["api_id"] <= 0
            or not re.fullmatch(r"[a-fA-F0-9]{32}", str(data.get("api_hash", "")))):
        raise ValueError("Invalid local API configuration")
    if data.get("authorized") and (type(data.get("user_id")) is not int or data["user_id"] <= 0):
        raise ValueError("Missing authorized account identity")
    path.chmod(0o600)
    return data


def save_credentials(data):
    folder = prepare_private()
    fd, temporary = tempfile.mkstemp(prefix=".credentials-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, folder / "credentials.json")
        (folder / "credentials.json").chmod(0o600)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def connection_lock():
    path = prepare_private() / "client.lock"
    with path.open("a") as stream:
        path.chmod(0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Сессия уже используется. Остановите свой MCP перед входом/проверкой.") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def access_profile():
    name = os.environ.get("AGENT_TELEGRAM_ACCESS", "read-only")
    if name not in PROFILES:
        raise ValueError("Unknown AGENT_TELEGRAM_ACCESS profile")
    return name


def proxy_config():
    """Only an explicitly selected, already running local SOCKS5 proxy."""
    host = os.environ.get("AGENT_TELEGRAM_SOCKS_HOST", "")
    port = os.environ.get("AGENT_TELEGRAM_SOCKS_PORT", "")
    if not host and not port:
        return None
    if host not in {"127.0.0.1", "localhost", "::1"} or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("Set both local SOCKS host and a valid port")
    return {"proxy_type": "socks5", "addr": host, "port": int(port), "rdns": True}


def configure_environment(data):
    profile = access_profile()
    proxy = proxy_config()
    for key in list(os.environ):
        if key.startswith("TELEGRAM_"):
            del os.environ[key]
    folder = prepare_private()
    os.environ.update({
        "PYTHON_DOTENV_DISABLED": "1",
        "TELEGRAM_API_ID": str(data["api_id"]),
        "TELEGRAM_API_HASH": data["api_hash"],
        "TELEGRAM_SESSION_NAME": str(folder / "account"),
        "TELEGRAM_DEVICE_MODEL": DEVICE,
        "TELEGRAM_SYSTEM_VERSION": "local",
        "TELEGRAM_APP_VERSION": "0.1.0",
        "TELEGRAM_EXPOSED_TOOLS": PROFILES[profile],
        "TELEGRAM_TRANSCRIBE": "off",
        "TELEGRAM_FLOOD_SLEEP_THRESHOLD": "0",
        "TELEGRAM_SERVER_ROOTS_ONLY": "1",
        "TELEGRAM_ALIASES_FILE": str(folder / "aliases.json"),
        "MCP_TRANSPORT": "stdio",
    })
    if proxy:
        os.environ.update(TELEGRAM_PROXY_TYPE="socks5", TELEGRAM_PROXY_HOST=proxy["addr"],
                          TELEGRAM_PROXY_PORT=str(proxy["port"]))
    if data.get("username"):
        os.environ["TELEGRAM_EXPECTED_USERNAME"] = data["username"]


def check_upstream():
    try:
        head = subprocess.check_output(["git", "-C", str(UPSTREAM), "rev-parse", "HEAD"], text=True).strip()
        dirty = subprocess.check_output(["git", "-C", str(UPSTREAM), "status", "--porcelain", "--untracked-files=no"], text=True).strip()
    except subprocess.CalledProcessError:
        raise RuntimeError("Сначала выполните bootstrap.py.") from None
    if head != UPSTREAM_COMMIT or dirty:
        raise RuntimeError("Upstream не совпадает с проверенной ревизией или изменён. Не запускаю.")

