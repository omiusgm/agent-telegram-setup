import asyncio
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telethon import errors
from telegram_setup import config, login


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"AGENT_TELEGRAM_HOME": self.temp.name}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_private_storage_and_no_extra_files(self):
        config.save_credentials({"api_id": 12345, "api_hash": "0" * 32, "authorized": False})
        self.assertEqual(config.load_credentials()["api_id"], 12345)
        self.assertEqual(stat.S_IMODE(config.private_dir().stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((config.private_dir() / "credentials.json").stat().st_mode), 0o600)
        self.assertEqual([p.name for p in config.private_dir().iterdir()], ["credentials.json"])

    def test_rejects_invalid_credentials_without_echo(self):
        marker = "synthetic-invalid-value"
        config.save_credentials({"api_id": 12345, "api_hash": marker})
        with self.assertRaises(ValueError) as error:
            config.load_credentials()
        self.assertNotIn(marker, str(error.exception))

    def test_authorized_record_requires_identity(self):
        config.save_credentials({"api_id": 12345, "api_hash": "0" * 32, "authorized": True})
        with self.assertRaises(ValueError):
            config.load_credentials()

    def test_concurrent_session_fails_closed(self):
        with config.connection_lock():
            with self.assertRaises(RuntimeError):
                with config.connection_lock():
                    self.fail("A second process must not own the session")
        with config.connection_lock():
            pass

    def test_default_read_only_and_parent_environment_is_not_inherited(self):
        os.environ.update(TELEGRAM_STRING_SESSION="synthetic-parent", TELEGRAM_EXPOSED_TOOLS="all")
        config.configure_environment({"api_id": 12345, "api_hash": "0" * 32})
        self.assertEqual(os.environ["TELEGRAM_EXPOSED_TOOLS"], "read-only")
        self.assertNotIn("TELEGRAM_STRING_SESSION", os.environ)
        self.assertEqual(os.environ["TELEGRAM_TRANSCRIBE"], "off")

    def test_unknown_profile_does_not_expand_access(self):
        os.environ["AGENT_TELEGRAM_ACCESS"] = "all"
        with self.assertRaises(ValueError):
            config.access_profile()

    def test_optional_proxy_requires_explicit_local_endpoint(self):
        self.assertIsNone(config.proxy_config())
        os.environ.update(AGENT_TELEGRAM_SOCKS_HOST="proxy.example.com", AGENT_TELEGRAM_SOCKS_PORT="1080")
        with self.assertRaises(ValueError):
            config.proxy_config()
        os.environ["AGENT_TELEGRAM_SOCKS_HOST"] = "127.0.0.1"
        self.assertEqual(config.proxy_config()["port"], 1080)


class QRTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_session_cannot_replace_expected_owner(self):
        me = type("User", (), {"bot": False, "id": 2000})()
        fake = type("Fake", (), {"connect": AsyncMock(), "is_user_authorized": AsyncMock(return_value=True),
                                  "get_me": AsyncMock(return_value=me), "disconnect": AsyncMock()})()
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {"AGENT_TELEGRAM_HOME": folder}, clear=True):
                with patch.object(login, "TelegramClient", return_value=fake), patch.object(login, "save_credentials") as save:
                    with self.assertRaises(RuntimeError):
                        await login.authorize({"api_id": 12345, "api_hash": "0" * 32, "user_id": 1000})
                    save.assert_not_called()
                    fake.disconnect.assert_awaited_once()

    async def test_listener_starts_before_render_and_expired_qr_refreshes(self):
        class QR:
            expires = datetime.now(timezone.utc) + timedelta(seconds=60)
            starts = 0
            refreshes = 0

            async def wait(self, timeout):
                self.starts += 1
                if self.starts == 1:
                    raise asyncio.TimeoutError()

            async def recreate(self):
                self.refreshes += 1

        qr = QR()
        client = type("Fake", (), {"qr_login": AsyncMock(return_value=qr)})()

        def render(current, path):
            self.assertGreater(current.starts, 0)

        with patch.object(login, "render_qr", side_effect=render), patch("builtins.print"):
            await login.qr_login(client)
        self.assertEqual(qr.refreshes, 1)
        self.assertEqual(qr.starts, 2)

    async def test_two_factor_branch(self):
        qr = type("FakeQR", (), {"expires": datetime.now(timezone.utc) + timedelta(seconds=60),
                                 "wait": AsyncMock(side_effect=errors.SessionPasswordNeededError(None))})()
        client = type("Fake", (), {"qr_login": AsyncMock(return_value=qr)})()
        with patch.object(login, "render_qr"), patch.object(login, "password_login", new_callable=AsyncMock) as password:
            with patch("builtins.print"):
                await login.qr_login(client)
            password.assert_awaited_once_with(client)


if __name__ == "__main__":
    unittest.main()

