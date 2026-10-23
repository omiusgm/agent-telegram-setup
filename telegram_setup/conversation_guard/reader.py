"""Offline snapshot reader and non-authoritative drafts. No credentials or writer."""
import contextlib
import json
import math
import os
import sqlite3
import stat
import time
import uuid
from pathlib import Path

from .policy import digest, validate_text
from .errors import Denied

MAX_STATE_BYTES = 2 * 1024 * 1024
RUNTIME_BLOCKER = "no_attested_reader_broker_writer_os_boundary"


def read_json(path):
    path = Path(path)
    if path.is_symlink():
        raise Denied("reader_state_symlink_rejected")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STATE_BYTES:
            raise Denied("invalid_reader_state_size_or_type")
        raw = stream.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise Denied("invalid_reader_state_size_or_type")
        return json.loads(raw)


def validate_snapshot(data, chat_id):
    if (not isinstance(data, dict) or set(data) != {"chat_id", "messages"}
            or type(data["chat_id"]) is not int or data["chat_id"] != chat_id
            or not isinstance(data["messages"], list) or len(data["messages"]) > 200):
        raise Denied("invalid_snapshot_scope_or_schema")
    ids = set()
    for m in data["messages"]:
        if (not isinstance(m, dict) or not {"id", "text"} <= set(m)
                or not set(m) <= {"id", "text", "sender", "date", "out", "media"}
                or type(m["id"]) is not int or m["id"] <= 0 or m["id"] in ids
                or not isinstance(m["text"], str) or len(m["text"]) > 6000
                or ("out" in m and type(m["out"]) is not bool)):
            raise Denied("invalid_snapshot_message")
        for key, limit in (("sender", 120), ("date", 64), ("media", 40)):
            if key in m and (not isinstance(m[key], str) or len(m[key]) > limit):
                raise Denied("invalid_snapshot_metadata")
        ids.add(m["id"])
    return data


def validate_policy(data):
    keys = {"version", "mode", "instance", "chat_id", "expires_at", "read_limit", "read_budget", "draft_ttl", "snapshot_sha256"}
    if isinstance(data, dict) and data.get("version") == 1:
        raise Denied("legacy_live_policy_disabled_runtime_unverified")
    if (not isinstance(data, dict) or set(data) != keys or type(data["version"]) is not int
            or data["version"] != 2 or data["mode"] != "snapshot-read-draft"):
        raise Denied("reader_policy_must_be_snapshot_read_draft")
    for key, low, high in (("chat_id", 1, 2**53-1), ("read_limit", 1, 50), ("read_budget", 1, 1440), ("draft_ttl", 30, 900)):
        if type(data[key]) is not int or not low <= data[key] <= high:
            raise Denied("invalid_reader_policy_" + key)
    if (type(data["expires_at"]) not in (int, float) or not math.isfinite(data["expires_at"])
            or not 0 < data["expires_at"] < 10**11 or not isinstance(data["instance"], str)
            or len(data["instance"]) != 32 or not isinstance(data["snapshot_sha256"], str)
            or len(data["snapshot_sha256"]) != 64):
        raise Denied("invalid_reader_policy_integrity")
    return data


def initialize(home, snapshot, chat_id, *, hours=4, read_budget=240, now=None):
    if type(chat_id) is not int or not 0 < chat_id < 2**53:
        raise Denied("invalid_private_chat_id")
    if type(hours) not in (int, float) or not 0 < hours <= 24:
        raise Denied("invalid_reader_duration")
    snapshot = validate_snapshot(snapshot, chat_id)
    if len(json.dumps(snapshot, ensure_ascii=False).encode()) > MAX_STATE_BYTES:
        raise Denied("snapshot_too_large")
    data = validate_policy({"version": 2, "mode": "snapshot-read-draft", "instance": uuid.uuid4().hex,
        "chat_id": chat_id, "expires_at": (time.time() if now is None else now) + hours * 3600,
        "read_limit": 30, "read_budget": read_budget, "draft_ttl": 900, "snapshot_sha256": digest(snapshot)})
    home = Path(home)
    if home.is_symlink() or (home.exists() and any(home.iterdir())):
        raise Denied("reader_home_must_be_new_or_empty")
    os.umask(0o077)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    for filename, value in (("snapshot.json", snapshot), ("policy.json", data)):
        target = home/filename
        with target.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
        target.chmod(0o600)
    return data


class DraftStore:
    """Worker-owned untrusted proposals, deliberately not the owner approval DB."""
    def __init__(self, home, policy, clock=time.time):
        self.policy, self.clock = policy, clock
        self.path = Path(home)/"drafts.sqlite3"
        if self.path.is_symlink():
            raise Denied("reader_state_symlink_rejected")
        os.umask(0o077)
        with self.connect() as db:
            db.executescript("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS drafts(id TEXT PRIMARY KEY,reply_id INTEGER NOT NULL,text TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL);")
            fingerprint = digest(policy)
            db.execute("INSERT OR IGNORE INTO meta VALUES ('policy',?)", (fingerprint,))
            if db.execute("SELECT value FROM meta WHERE key='policy'").fetchone()[0] != fingerprint:
                raise Denied("reader_policy_changed")
        self.path.chmod(0o600)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def check(self, db):
        if self.clock() >= self.policy["expires_at"]:
            raise Denied("reader_expired")
        if db.execute("SELECT 1 FROM meta WHERE key='revoked'").fetchone():
            raise Denied("reader_revoked")

    def reserve_read(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.check(db)
            row = db.execute("SELECT value FROM meta WHERE key='reads'").fetchone()
            count = int(row[0]) if row else 0
            if count >= self.policy["read_budget"]:
                raise Denied("read_budget_exhausted")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('reads',?)", (str(count+1),))

    def propose(self, reply_id, text):
        validate_text(text)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.check(db)
            if db.execute("SELECT count(*) FROM drafts").fetchone()[0] >= 20:
                raise Denied("draft_budget_exhausted")
            ident, now = uuid.uuid4().hex, self.clock()
            db.execute("INSERT INTO drafts VALUES (?,?,?,?,?)", (ident, reply_id, text, now, min(now+self.policy["draft_ttl"], self.policy["expires_at"])))
        return self.get(ident)

    def get(self, ident):
        with self.connect() as db:
            row = db.execute("SELECT * FROM drafts WHERE id=?", (ident,)).fetchone()
            if row is None:
                raise Denied("unknown_draft")
            return {"action_id": row["id"], "status": "draft" if self.clock() < row["expires"] else "expired",
                "text": row["text"], "in_reply_to": row["reply_id"], "expires_at": row["expires"],
                "external_writes_enabled": False, "owner_approval_record": False}

    def revoke(self):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('revoked','true')")

    def summary(self):
        return {"mode": "snapshot-read-draft", "chat_id": self.policy["chat_id"], "expires_at": self.policy["expires_at"],
            "secure": False, "host_isolation_verified": False, "runtime_blocker": RUNTIME_BLOCKER,
            "external_reads_enabled": False, "external_writes_enabled": False, "credentials_loaded": False,
            "approval_api_exposed": False, "drafts_are_authority": False}


class SnapshotReader:
    publisher_status = "disabled_no_verified_runtime"

    def __init__(self, home, clock=time.time):
        home = Path(home)
        if home.is_symlink():
            raise Denied("reader_home_symlink_rejected")
        self.policy = validate_policy(read_json(home/"policy.json"))
        self.snapshot = validate_snapshot(read_json(home/"snapshot.json"), self.policy["chat_id"])
        if digest(self.snapshot) != self.policy["snapshot_sha256"]:
            raise Denied("snapshot_changed_after_initialization")
        self.store = DraftStore(home, self.policy, clock)
        self.seen = set()

    async def read(self):
        self.store.reserve_read()
        selected = sorted(self.snapshot["messages"], key=lambda m:m["id"], reverse=True)[:self.policy["read_limit"]]
        self.seen.update(m["id"] for m in selected if m.get("out") is not True)
        return {"trust": "untrusted", "source": "owner_supplied_local_snapshot", "messages": [
            {"message_id": m["id"], "source_chat_id": self.policy["chat_id"], "sender_label": m.get("sender", "Unknown"),
             "body": m["text"], "trust": "untrusted", "own_message": m.get("out") is True} for m in selected],
            "external_reads_enabled": False, "external_writes_enabled": False, "attachments_processed": False}

    def propose(self, reply_id, text):
        if type(reply_id) is not int or reply_id not in self.seen:
            raise Denied("draft_requires_observed_incoming_message")
        return self.store.propose(reply_id, text)

    def view(self, ident):
        return self.store.get(ident)
