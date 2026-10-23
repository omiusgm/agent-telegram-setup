"""Owner approvals and atomic action claims live outside model-supplied content."""
import contextlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from .policy import canonical, digest, validate_text
from .errors import Denied


class Store:
    def __init__(self, home, policy, clock=time.time):
        self.home, self.policy, self.clock = Path(home), policy, clock
        self.fingerprint = digest(policy)
        self.path = self.home / "actions.sqlite3"
        os.umask(0o077)
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.home.chmod(0o700)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observed (id INTEGER PRIMARY KEY, incoming INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS actions (
                    id TEXT PRIMARY KEY, intent TEXT NOT NULL, hash TEXT UNIQUE NOT NULL, payload TEXT NOT NULL,
                    reply_id INTEGER NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                    status TEXT NOT NULL, result_id INTEGER, reason TEXT);
                CREATE TABLE IF NOT EXISTS deliveries (reply_id INTEGER PRIMARY KEY, action_id TEXT UNIQUE NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO meta VALUES ('policy', ?)", (self.fingerprint,))
            if db.execute("SELECT value FROM meta WHERE key='policy'").fetchone()[0] != self.fingerprint:
                raise Denied("policy_changed_use_new_guard_home")
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

    @contextlib.contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def check(self, db):
        if self.clock() >= self.policy["expires_at"]:
            raise Denied("delegation_expired")
        if db.execute("SELECT 1 FROM meta WHERE key='revoked'").fetchone():
            raise Denied("delegation_revoked")

    def observe(self, messages):
        with self.transaction() as db:
            self.check(db)
            db.execute("UPDATE actions SET status='expired',reason='draft_expired' WHERE status IN ('pending','approved') AND expires<=?", (self.clock(),))
            for m in messages:
                if type(m.get("id")) is int and m["id"] > 0:
                    db.execute("INSERT OR REPLACE INTO observed VALUES (?,?)", (m["id"], int(not m.get("out", False))))

    def reserve_read(self):
        with self.transaction() as db:
            self.check(db)
            row = db.execute("SELECT value FROM meta WHERE key='reads'").fetchone()
            count = int(row[0]) if row else 0
            if count >= self.policy["read_budget"]:
                raise Denied("read_budget_exhausted")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('reads',?)", (str(count+1),))

    def propose(self, reply_id, text, *, template=None):
        validate_text(text)
        if type(reply_id) is not int or reply_id <= 0:
            raise Denied("invalid_reply_id")
        with self.transaction() as db:
            self.check(db)
            db.execute("UPDATE actions SET status='expired',reason='draft_expired' WHERE status IN ('pending','approved') AND expires<=?", (self.clock(),))
            seen = db.execute("SELECT incoming FROM observed WHERE id=?", (reply_id,)).fetchone()
            if seen is None or not seen[0]:
                raise Denied("reply_requires_observed_incoming_message")
            if db.execute("SELECT 1 FROM deliveries WHERE reply_id=?", (reply_id,)).fetchone():
                raise Denied("already_dispatched_do_not_retry")
            if template is not None and self.policy["templates"].get(template) != text:
                raise Denied("template_not_preapproved")
            payload = {"policy": self.fingerprint, "tool": "reply_to_message", "chat_id": self.policy["chat_id"],
                       "message_id": reply_id, "text": text, "parse_mode": None, "template": template}
            intent = digest(payload)
            existing = db.execute("SELECT id FROM actions WHERE intent=? AND status IN ('pending','approved') AND expires>?", (intent, self.clock())).fetchone()
            if existing:
                return self.get(existing[0])
            if db.execute("SELECT count(*) FROM actions WHERE status IN ('pending','approved')").fetchone()[0] >= 20:
                raise Denied("pending_queue_full")
            ident = uuid.uuid4().hex
            now = self.clock()
            expiry = min(now+self.policy["draft_ttl"], self.policy["expires_at"])
            payload.update(action_id=ident, created_at=now, expires_at=expiry)
            action_hash = digest(payload)
            status = "approved" if template is not None else "pending"
            db.execute("INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?,?)", (
                ident, intent, action_hash, canonical(payload), reply_id, now, expiry, status, None, None))
        return self.get(ident)

    def get(self, ident):
        with self.connect() as db:
            row = db.execute("SELECT * FROM actions WHERE id=?", (ident,)).fetchone()
            if row is None:
                raise Denied("unknown_action")
            value = dict(row)
            value["payload"] = json.loads(value["payload"])
            return value

    def approve(self, ident, action_hash):
        with self.transaction() as db:
            self.check(db)
            row = db.execute("SELECT * FROM actions WHERE id=?", (ident,)).fetchone()
            if row is None or row["status"] != "pending" or row["expires"] <= self.clock():
                raise Denied("approval_missing_stale_or_consumed")
            payload = json.loads(row["payload"])
            if action_hash != row["hash"] or not self.integrity(row):
                raise Denied("approval_action_mismatch")
            db.execute("UPDATE actions SET status='approved' WHERE id=?", (ident,))

    def claim(self):
        with self.transaction() as db:
            self.check(db)
            if db.execute("SELECT count(*) FROM deliveries").fetchone()[0] >= self.policy["send_budget"]:
                return None
            for row in db.execute("SELECT * FROM actions WHERE status='approved' ORDER BY created").fetchall():
                if row["expires"] <= self.clock() or not self.integrity(row):
                    db.execute("UPDATE actions SET status='rejected',reason='stale_or_changed' WHERE id=?", (row["id"],))
                    continue
                payload = json.loads(row["payload"])
                if (payload.get("policy") != self.fingerprint or payload.get("chat_id") != self.policy["chat_id"]
                        or payload.get("tool") != "reply_to_message" or payload.get("message_id") != row["reply_id"]
                        or payload.get("parse_mode") is not None):
                    db.execute("UPDATE actions SET status='rejected',reason='scope_mismatch' WHERE id=?", (row["id"],))
                    continue
                try:
                    db.execute("INSERT INTO deliveries VALUES (?,?)", (row["reply_id"], row["id"]))
                except sqlite3.IntegrityError:
                    db.execute("UPDATE actions SET status='rejected',reason='reply_already_claimed' WHERE id=?", (row["id"],))
                    continue
                db.execute("UPDATE actions SET status='sending' WHERE id=?", (row["id"],))
                return {"id": row["id"], "payload": payload}
        return None

    def finish(self, ident, *, message_id=None, reason=None):
        with self.transaction() as db:
            db.execute("UPDATE actions SET status=?,result_id=?,reason=? WHERE id=? AND status='sending'",
                       ("sent" if message_id is not None else "uncertain_do_not_retry", message_id, reason, ident))

    def validate_before_send(self, ident):
        """Recheck after asynchronous preparation, immediately before the write."""
        with self.transaction() as db:
            self.check(db)
            row = db.execute("SELECT * FROM actions WHERE id=?", (ident,)).fetchone()
            if (row is None or row["status"] != "sending" or row["expires"] <= self.clock()
                    or not self.integrity(row)):
                raise Denied("claimed_action_stale_or_changed")

    def cancel_before_send(self, ident, reason):
        with self.transaction() as db:
            db.execute("UPDATE actions SET status='rejected',reason=? WHERE id=? AND status='sending'", (reason, ident))

    def integrity(self, row):
        try:
            payload = json.loads(row["payload"])
            return (digest(payload) == row["hash"] and payload.get("action_id") == row["id"]
                    and payload.get("created_at") == row["created"] and payload.get("expires_at") == row["expires"])
        except (ValueError, TypeError):
            return False

    def reject(self, ident):
        with self.transaction() as db:
            db.execute("UPDATE actions SET status='rejected',reason='owner_rejected' WHERE id=? AND status IN ('pending','approved')", (ident,))

    def recover(self):
        with self.transaction() as db:
            db.execute("UPDATE actions SET status='uncertain_do_not_retry',reason='broker_interrupted' WHERE status='sending'")

    def revoke(self):
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('revoked','true')")

    def summary(self):
        with self.connect() as db:
            return {"scope": "one_private_chat", "chat_id": self.policy["chat_id"],
                    "expires_at": self.policy["expires_at"], "revoked": bool(db.execute("SELECT 1 FROM meta WHERE key='revoked'").fetchone()),
                    "budget": self.policy["send_budget"], "attempted": db.execute("SELECT count(*) FROM deliveries").fetchone()[0],
                    "read_budget": self.policy["read_budget"], "reads_used": int((db.execute("SELECT value FROM meta WHERE key='reads'").fetchone() or [0])[0]),
                    "templates": list(self.policy["templates"]), "owner_approval_required_for_free_text": True,
                    "host_isolation_verified": False}
