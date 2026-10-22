"""Start the pinned upstream over stdio with a single local session."""

import asyncio
import os
import sys

from .config import ROOT, UPSTREAM, check_upstream, configure_environment, connection_lock, load_credentials, prepare_private
from .access_guard import enforce_transcription_engine
from .permissions import load_settings


def prepare():
    check_upstream()
    data = load_credentials()
    if not data.get("authorized"):
        raise RuntimeError("Сначала завершите локальный вход: python -m telegram_setup.login")
    configure_environment(data)
    files = prepare_private() / "files"
    files.mkdir(exist_ok=True, mode=0o700)
    files.chmod(0o700)
    os.chdir(UPSTREAM)
    sys.path.insert(0, str(UPSTREAM))
    sys.argv = [str(UPSTREAM / "main.py"), str(files)]
    from telegram_mcp import runner
    from telegram_mcp import tools  # noqa: F401; register tools before filtering
    from telegram_mcp.install_guard import assert_safe_distribution
    assert_safe_distribution()
    enforce_transcription_engine(runner.mcp, load_settings())

    async def serve_checked():
        try:
            for client in runner.clients.values():
                await asyncio.wait_for(client.connect(), timeout=45)
                if not await client.is_user_authorized():
                    raise RuntimeError("Сессия больше не авторизована. Выполните локальный вход.")
                me = await client.get_me()
                if me.bot or me.id != data["user_id"]:
                    raise RuntimeError("Аккаунт не совпадает с сохранённым владельцем.")
            # No full-dialog cache warmer: read only on an explicit MCP call.
            await runner._serve("stdio")
        finally:
            await asyncio.gather(*(client.disconnect() for client in runner.clients.values()), return_exceptions=True)
            for lock in runner._session_locks.values():
                lock.release()
            runner._session_locks.clear()

    runner._main = serve_checked
    return runner


def main():
    try:
        with connection_lock():
            prepare().main()
        return 0
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print("MCP не запущен; тип ошибки: " + type(exc).__name__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

