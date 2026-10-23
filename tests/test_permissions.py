import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

import bootstrap
from telegram_setup import config, permissions
from telegram_setup.access_guard import enforce_transcription_engine, enforce_permission_snapshot


def selection(*enabled, engine=None):
    return {"schema_version": 1, "enabled": list(enabled), "transcription_engine": engine}


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"AGENT_TELEGRAM_HOME": self.temp.name}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_first_install_requires_an_explicit_choice(self):
        with self.assertRaises(RuntimeError):
            permissions.load_settings()
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_durable_choice_does_not_change_account_files(self):
        account = Path(self.temp.name) / "account.session"
        account.write_bytes(b"synthetic-existing-account")
        settings = permissions.save_settings(selection("read", "download"))
        self.assertEqual(permissions.load_settings(), settings)
        self.assertEqual(account.read_bytes(), b"synthetic-existing-account")
        self.assertEqual(stat.S_IMODE((Path(self.temp.name) / "permissions.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(Path(self.temp.name).stat().st_mode), 0o700)

    def test_invalid_selection_preserves_previous_choice(self):
        permissions.save_settings(selection("read", "download"))
        for data in (selection("read", "unknown"), selection("read", "read"), selection("send-text"),
                     selection("read", "transcribe"), selection("read", engine="groq")):
            with self.subTest(data=data), self.assertRaises(ValueError):
                permissions.save_settings(data)
        self.assertEqual(permissions.load_settings()["enabled"], ["read", "download"])

    def test_legacy_profiles_remain_explicit_and_saved_choice_wins(self):
        for name, enabled in permissions.LEGACY.items():
            os.environ["AGENT_TELEGRAM_ACCESS"] = name
            self.assertEqual(permissions.load_settings()["enabled"], enabled)
        permissions.save_settings(selection("read", "download"))
        self.assertEqual(config.access_profile(), "custom")
        self.assertEqual(permissions.load_settings()["enabled"], ["read", "download"])

    def test_wizard_retries_blank_and_invalid_then_saves_only_selection(self):
        answers = iter(["", "999", "2", "да"])
        with patch("builtins.print"):
            selected = permissions.choose_interactively(lambda _: next(answers))
        self.assertEqual(selected, selection("read", "download"))
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_wizard_cancellation_does_not_save(self):
        answers = iter(["0", "нет"])
        with patch("builtins.print"):
            self.assertIsNone(permissions.choose_interactively(lambda _: next(answers)))
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_wizard_requires_transcription_destination(self):
        answers = iter(["3", "0", "1", "да"])
        with patch("builtins.print"):
            selected = permissions.choose_interactively(lambda _: next(answers))
        self.assertEqual(selected, selection("read", "transcribe", engine="telegram"))

    def test_noninteractive_explicit_choice_and_safe_show(self):
        with patch("builtins.print"):
            self.assertEqual(permissions.main(["--allow", "read,download", "--yes"]), 0)
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(permissions.main(["--show"]), 0)
        self.assertIn("download_media", json.loads(output.getvalue())["tools_filter"])
        self.assertNotIn("api_hash", output.getvalue())

    def test_noninteractive_no_choice_does_not_silently_pick_a_profile(self):
        with patch("sys.stdin.isatty", return_value=False), patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit):
                permissions.main([])
            with patch("sys.argv", ["bootstrap.py"]), patch.object(bootstrap.shutil, "which") as lookup:
                with self.assertRaises(SystemExit):
                    bootstrap.main()
                lookup.assert_not_called()
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_only_selected_provider_configuration_is_preserved(self):
        os.environ.update(TELEGRAM_STRING_SESSION="synthetic-unwanted-session",
                          TELEGRAM_TRANSCRIBE_OPENAI_URL="https://example.invalid/audio/transcriptions",
                          TELEGRAM_TRANSCRIBE_OPENAI_API_KEY="synthetic-key")
        permissions.save_settings(selection("read", "transcribe", engine="openai"))
        config.configure_environment({"api_id": 12345, "api_hash": "0" * 32})
        self.assertIn("TELEGRAM_TRANSCRIBE_OPENAI_URL", os.environ)
        self.assertNotIn("TELEGRAM_STRING_SESSION", os.environ)
        permissions.save_settings(selection("read"))
        config.configure_environment({"api_id": 12345, "api_hash": "0" * 32})
        self.assertNotIn("TELEGRAM_TRANSCRIBE_OPENAI_API_KEY", os.environ)
        self.assertEqual(os.environ["TELEGRAM_TRANSCRIBE"], "off")

    def test_bootstrap_asks_before_installing_and_persists_the_answer(self):
        chosen = selection("read", "download")
        with patch("sys.argv", ["bootstrap.py"]), patch("sys.stdin.isatty", return_value=True), \
                patch.object(permissions, "choose_interactively", return_value=chosen) as question, \
                patch.object(bootstrap.shutil, "which", return_value="synthetic-executable"), \
                patch.object(bootstrap, "check_upstream"), patch.object(bootstrap.subprocess, "run"), \
                patch("builtins.print"):
            bootstrap.main()
        question.assert_called_once()
        self.assertEqual(permissions.load_settings(), chosen)

    def test_bootstrap_reuses_saved_choice_without_asking_again(self):
        chosen = permissions.save_settings(selection("read", "send-files"))
        with patch("sys.argv", ["bootstrap.py"]), \
                patch.object(permissions, "choose_interactively") as question, \
                patch.object(bootstrap.shutil, "which", return_value="synthetic-executable"), \
                patch.object(bootstrap, "check_upstream"), patch.object(bootstrap.subprocess, "run"), \
                patch("builtins.print"):
            bootstrap.main()
        question.assert_not_called()
        self.assertEqual(permissions.load_settings(), chosen)


class SharedPermissionSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_revoked_permission_blocks_a_previously_exposed_operation(self):
        from mcp.server.fastmcp.exceptions import ToolError
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ,
                {'AGENT_TELEGRAM_HOME': home}, clear=True):
            chosen = permissions.save_settings(selection('read', 'send-text'))
            server = FastMCP('synthetic-shared-permissions')
            operation = AsyncMock(return_value='synthetic-sent')

            @server.tool()
            async def send_message(text: str) -> str:
                return await operation(text)

            enforce_permission_snapshot(server, chosen)
            await server.call_tool('send_message', {'text': 'Before revocation'})
            operation.assert_awaited_once_with('Before revocation')
            operation.reset_mock()
            permissions.save_settings(selection('read'))
            with self.assertRaisesRegex(ToolError, 'Permissions changed'):
                await server.call_tool('send_message', {'text': 'Must not execute'})
            operation.assert_not_awaited()

    async def test_missing_or_corrupt_permission_file_fails_before_operation(self):
        from mcp.server.fastmcp.exceptions import ToolError
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ,
                {'AGENT_TELEGRAM_HOME': home}, clear=True):
            chosen = permissions.save_settings(selection('read'))
            server = FastMCP('synthetic-invalid-permissions')
            operation = AsyncMock(return_value='synthetic-read')

            @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
            async def synthetic_read() -> str:
                return await operation()

            enforce_permission_snapshot(server, chosen)
            path = Path(home) / 'permissions.json'
            for content in ('invalid JSON', None):
                if content is None:
                    path.unlink()
                else:
                    path.write_text(content)
                with self.subTest(content=content), self.assertRaisesRegex(ToolError, 'Permissions unavailable'):
                    await server.call_tool('synthetic_read', {})
            operation.assert_not_awaited()


class TranscriptionGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_engine_override_is_rejected_before_provider_call(self):
        provider = AsyncMock(return_value="synthetic-transcript")
        server = FastMCP("synthetic-transcription")

        @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
        async def transcribe_voice(chat_id: str, message_id: int, engine: str = None, account: str = None) -> str:
            return await provider(chat_id=chat_id, message_id=message_id, engine=engine, account=account)

        enforce_transcription_engine(server, selection("read", "transcribe", engine="telegram"))
        guarded = server._tool_manager.get_tool("transcribe_voice").fn
        result = json.loads(await guarded(chat_id="me", message_id=1, engine="groq"))
        self.assertEqual(result["reason"], "transcription_engine_not_permitted")
        provider.assert_not_called()
        self.assertEqual(await guarded(chat_id="me", message_id=1), "synthetic-transcript")
        provider.assert_awaited_once_with(chat_id="me", message_id=1, engine="telegram", account=None)


if __name__ == "__main__":
    unittest.main()
