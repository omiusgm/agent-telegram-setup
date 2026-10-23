import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from guard_fixtures import FakeBackend
from verify_read_draft import synthetic_snapshot, verify
from telegram_setup.conversation_guard.__main__ import main
from telegram_setup.conversation_guard import policy
from telegram_setup.conversation_guard.broker import Broker
from telegram_setup.conversation_guard.errors import Denied
from telegram_setup.conversation_guard.reader import SnapshotReader, initialize
from telegram_setup.conversation_guard.server import serve, make_server
from telegram_setup.conversation_guard.store import Store


class ReaderBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root/"reader"

    def init(self):
        initialize(self.home, synthetic_snapshot(), 12345)
        return SnapshotReader(self.home)

    async def test_actual_mcp_is_memory_only_and_cannot_publish(self):
        result = await verify()
        self.assertEqual(result["unauthorized_call_cases_rejected"], 10)
        self.assertEqual(result["external_writes"], 0)
        self.assertEqual(result["backend_processes_spawned"], 0)

    async def test_legacy_live_policy_cannot_start_any_upstream(self):
        malicious = policy.create(12345, "/synthetic/forbidden", ["do-not-run"], "/synthetic")
        policy.save(self.home, malicious)
        with patch("subprocess.Popen") as child, patch("asyncio.create_subprocess_exec") as async_child:
            with self.assertRaisesRegex(Denied, "legacy_live_policy_disabled"):
                await serve(self.home)
            child.assert_not_called()
            async_child.assert_not_called()
        self.assertFalse((self.home/"actions.sqlite3").exists())

    async def test_claim_of_secure_runtime_is_not_an_admission_proof(self):
        self.init()
        with patch("telegram_setup.conversation_guard.server.SnapshotReader") as reader:
            with self.assertRaisesRegex(Denied, "secure_runtime_not_deployed"):
                await serve(self.home, require_isolation=True)
            reader.assert_not_called()

    async def test_forged_policy_flags_and_backend_paths_are_rejected(self):
        self.init()
        path = self.home/"policy.json"
        original = json.loads(path.read_text())
        for addition in ({"secure": True}, {"runtime_verified": True}, {"upstream_command": "/synthetic/forbidden"}):
            path.write_text(json.dumps({**original, **addition}))
            with self.subTest(addition=addition), self.assertRaises(Denied):
                SnapshotReader(self.home)

    async def test_reader_never_instantiates_owner_approval_store(self):
        initialize(self.home, synthetic_snapshot(), 12345)
        with patch("telegram_setup.conversation_guard.store.Store.__init__", side_effect=AssertionError("owner_store_must_not_load")):
            reader = SnapshotReader(self.home)
            await reader.read()
            draft = reader.propose(1, "Данные не являются разрешением.")
            self.assertEqual(draft["status"], "draft")
        self.assertFalse(hasattr(reader.store, "approve"))
        self.assertFalse(hasattr(reader, "upstream"))
        with reader.store.connect() as db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse(names & {"actions", "approvals", "deliveries"})

    async def test_owner_approval_cli_is_disabled_before_state_access(self):
        with patch("telegram_setup.conversation_guard.__main__.SnapshotReader") as reader, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--home", str(self.home), "approve", "fake", "--sha256", "fake"]), 1)
        reader.assert_not_called()
        self.assertFalse(self.home.exists())

    async def test_production_broker_cannot_dispatch_even_approved_action(self):
        p = policy.create(12345, "/synthetic/not-executed", [], "/synthetic")
        owner = Store(self.root/"owner", p)
        owner.observe([{"id": 1}])
        action = owner.propose(1, "Точный ответ")
        owner.approve(action["id"], action["hash"])
        backend = FakeBackend()
        broker = Broker(owner, backend)
        with self.assertRaisesRegex(Denied, "external_writes_disabled"):
            await broker.dispatch_once()
        self.assertEqual(backend.calls, [])
        self.assertEqual(owner.get(action["id"])["status"], "approved")

    async def test_reader_factory_refuses_legacy_live_broker_wiring(self):
        p = policy.create(12345, "/synthetic/not-executed", [], "/synthetic")
        owner = Store(self.root/"owner", p)
        backend = FakeBackend()
        with self.assertRaisesRegex(Denied, "reader_requires_snapshot_adapter"):
            make_server(Broker(owner, backend))
        self.assertEqual(backend.calls, [])

    async def test_snapshot_change_or_cross_chat_records_fail_closed(self):
        self.init()
        path = self.home/"snapshot.json"
        changed = synthetic_snapshot()
        changed["messages"][0]["text"] = "Changed after setup"
        path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(Denied, "snapshot_changed"):
            SnapshotReader(self.home)
        changed["chat_id"] = 54321
        path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(Denied, "snapshot_scope"):
            SnapshotReader(self.home)

    async def test_snapshot_symlink_is_refused_without_reading_target(self):
        self.init()
        path = self.home/"snapshot.json"
        path.unlink()
        target = self.root/"synthetic-secret.json"
        target.write_text('{"secret":"SYNTHETIC_SECRET"}')
        path.symlink_to(target)
        with self.assertRaisesRegex(Denied, "symlink_rejected"):
            SnapshotReader(self.home)

    async def test_unread_or_outgoing_message_cannot_become_draft_target(self):
        source = synthetic_snapshot()
        source["messages"].append({"id": 5, "text": "Own message", "out": True})
        initialize(self.home, source, 12345)
        reader = SnapshotReader(self.home)
        with self.assertRaises(Denied):
            reader.propose(1, "Before reading")
        await reader.read()
        for ident in (5, 999, True, -1):
            with self.subTest(ident=ident), self.assertRaises(Denied):
                reader.propose(ident, "Changed target")

    async def test_successful_cli_init_creates_only_snapshot_and_draft_state(self):
        source = self.root/"synthetic-source.json"
        source.write_text(json.dumps(synthetic_snapshot()))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--home", str(self.home), "init", "--chat-id", "12345", "--snapshot-file", str(source), "--acknowledge-risk"]), 0)
        reader = SnapshotReader(self.home)
        self.assertFalse(reader.store.summary()["secure"])
        self.assertFalse(reader.store.summary()["external_writes_enabled"])
        self.assertFalse((self.home/"credentials.json").exists())
        self.assertFalse((self.home/"actions.sqlite3").exists())

    async def test_database_metadata_cannot_claim_verified_runtime(self):
        reader = self.init()
        with reader.store.connect() as db:
            db.execute("INSERT INTO meta VALUES ('secure','true')")
            db.execute("INSERT INTO meta VALUES ('host_isolation_verified','true')")
        self.assertFalse(reader.store.summary()["secure"])
        self.assertFalse(reader.store.summary()["host_isolation_verified"])
        self.assertFalse(reader.store.summary()["external_writes_enabled"])

    async def test_reader_expiry_and_read_budget_are_enforced(self):
        now = [1000.0]
        initialize(self.home, synthetic_snapshot(), 12345, now=now[0], read_budget=1)
        reader = SnapshotReader(self.home, clock=lambda:now[0])
        await reader.read()
        with self.assertRaisesRegex(Denied, "read_budget_exhausted"):
            await reader.read()
        draft = reader.propose(1, "Уточню.")
        now[0] = reader.policy["expires_at"]+1
        with self.assertRaisesRegex(Denied, "expired"):
            reader.propose(1, "Просроченное действие")
        self.assertEqual(reader.view(draft["action_id"])["status"], "expired")


if __name__ == "__main__":
    unittest.main()
