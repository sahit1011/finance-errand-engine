"""SQLite storage for everything the engine must remember across a restart."""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict

from errand.core.domain import TERMINAL_STATES, Approval, Attempt, Run, State

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, user_id TEXT, state TEXT, seq INTEGER, data TEXT);
CREATE TABLE IF NOT EXISTS transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, from_state TEXT, to_state TEXT, reason TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, run_id TEXT, text TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS holds (
    hold_id TEXT PRIMARY KEY, run_id TEXT, provider_id TEXT, slot TEXT, quoted_price INTEGER, expires_at TEXT);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY, run_id TEXT, user_id TEXT, action TEXT, provider_id TEXT, provider_name TEXT, slot TEXT,
    price INTEGER, price_label TEXT, booking_id TEXT, status TEXT, idempotency_key TEXT,
    source_message_id INTEGER, shown_reply_id INTEGER, hold_id TEXT);
CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY, approval_id TEXT UNIQUE, idempotency_key TEXT, hold_id TEXT, status TEXT, retries INTEGER,
    returned_booking_id TEXT, contradiction_seen INTEGER);
CREATE TABLE IF NOT EXISTS booking_prices (booking_id TEXT PRIMARY KEY, price INTEGER);
CREATE TABLE IF NOT EXISTS memory_facts (
    user_id TEXT, key TEXT, value TEXT, provenance TEXT, status TEXT, evidence TEXT, updated_at TEXT,
    PRIMARY KEY (user_id, key));
CREATE TABLE IF NOT EXISTS replies (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, run_id TEXT, text TEXT, delivered INTEGER);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER);
"""

_RUN_FIELDS_OUTSIDE_DATA = ("id", "user_id", "state")


class Store:
    def __init__(self, path: str, clock):
        self.clock = clock
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._tx_depth = 0

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def tx(self):
        """One transaction; a nested use joins the outer one, so a group of saves is all or nothing."""
        if self._tx_depth == 0:
            self._db.execute("BEGIN IMMEDIATE")
        self._tx_depth += 1
        try:
            yield
        except BaseException:
            self._tx_depth -= 1
            if self._tx_depth == 0:
                self._db.execute("ROLLBACK")
            raise
        self._tx_depth -= 1
        if self._tx_depth == 0:
            self._db.execute("COMMIT")

    def next_number(self, name: str) -> int:
        self._db.execute(
            "INSERT INTO counters (name, value) VALUES (?, 1) ON CONFLICT(name) DO UPDATE SET value = value + 1", (name,)
        )
        return self._db.execute("SELECT value FROM counters WHERE name = ?", (name,)).fetchone()["value"]

    # Runs.

    def create_run(self, user_id: str, workflow: str) -> Run:
        run = Run(id=f"run-{self.next_number('runs')}", user_id=user_id, state=State.COLLECTING, workflow=workflow)
        seq = self.next_number("run_seq")
        self._db.execute(
            "INSERT INTO runs (id, user_id, state, seq, data) VALUES (?, ?, ?, ?, ?)",
            (run.id, user_id, run.state.value, seq, self._run_data(run)),
        )
        return run

    def save_run(self, run: Run) -> None:
        self._db.execute("UPDATE runs SET state = ?, data = ? WHERE id = ?", (run.state.value, self._run_data(run), run.id))

    def active_run(self, user_id: str) -> Run | None:
        for row in self._db.execute("SELECT * FROM runs WHERE user_id = ? ORDER BY seq DESC", (user_id,)).fetchall():
            if State(row["state"]) not in TERMINAL_STATES:
                return self._load_run(row)
        return None

    def latest_run(self, user_id: str) -> Run | None:
        row = self._db.execute("SELECT * FROM runs WHERE user_id = ? ORDER BY seq DESC LIMIT 1", (user_id,)).fetchone()
        return None if row is None else self._load_run(row)

    def record_transition(self, run_id: str, from_state: State, to_state: State, reason: str) -> None:
        self._db.execute(
            "INSERT INTO transitions (run_id, from_state, to_state, reason, at) VALUES (?, ?, ?, ?, ?)",
            (run_id, from_state.value, to_state.value, reason, self.clock.now().isoformat()),
        )

    # Messages and replies.

    def save_message(self, user_id: str, run_id: str | None, text: str) -> int:
        cursor = self._db.execute(
            "INSERT INTO messages (user_id, run_id, text, at) VALUES (?, ?, ?, ?)",
            (user_id, run_id, text, self.clock.now().isoformat()),
        )
        return cursor.lastrowid

    def save_reply(self, user_id: str, run_id: str | None, text: str) -> int:
        cursor = self._db.execute(
            "INSERT INTO replies (user_id, run_id, text, delivered) VALUES (?, ?, ?, 0)", (user_id, run_id, text)
        )
        return cursor.lastrowid

    def mark_delivered(self, reply_id: int) -> None:
        self._db.execute("UPDATE replies SET delivered = 1 WHERE id = ?", (reply_id,))

    def undelivered_replies(self, user_id: str) -> list[dict]:
        rows = self._db.execute("SELECT * FROM replies WHERE user_id = ? AND delivered = 0 ORDER BY id", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    # Holds.

    def save_hold(self, hold_id: str, run_id: str, provider_id: str, slot: str, quoted_price: int, expires_at: str) -> None:
        self._db.execute(
            "INSERT INTO holds (hold_id, run_id, provider_id, slot, quoted_price, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (hold_id, run_id, provider_id, slot, quoted_price, expires_at),
        )

    def hold(self, hold_id: str) -> dict | None:
        row = self._db.execute("SELECT * FROM holds WHERE hold_id = ?", (hold_id,)).fetchone()
        return None if row is None else dict(row)

    def active_hold(self, run_id: str, provider_id: str, slot: str, now: str) -> dict | None:
        row = self._db.execute(
            "SELECT * FROM holds WHERE run_id = ? AND provider_id = ? AND slot = ? AND expires_at > ? ORDER BY rowid DESC LIMIT 1",
            (run_id, provider_id, slot, now),
        ).fetchone()
        return None if row is None else dict(row)

    # Approvals and attempts.

    def insert_approval(self, approval: Approval, hold_id: str | None) -> None:
        values = asdict(approval)
        self._db.execute(
            """INSERT INTO approvals (id, run_id, user_id, action, provider_id, provider_name, slot, price, price_label,
               booking_id, status, idempotency_key, source_message_id, shown_reply_id, hold_id)
               VALUES (:id, :run_id, :user_id, :action, :provider_id, :provider_name, :slot, :price, :price_label,
               :booking_id, :status, :idempotency_key, :source_message_id, :shown_reply_id, :hold_id)""",
            {**values, "hold_id": hold_id},
        )

    def approval(self, approval_id: str) -> Approval:
        row = self._db.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return self._to_approval(row)

    def all_approvals(self) -> list[Approval]:
        return [self._to_approval(r) for r in self._db.execute("SELECT * FROM approvals ORDER BY rowid").fetchall()]

    def approval_hold_id(self, approval_id: str) -> str | None:
        return self._db.execute("SELECT hold_id FROM approvals WHERE id = ?", (approval_id,)).fetchone()["hold_id"]

    def mark_approved(self, approval_id: str, message_id: int, key: str) -> bool:
        """Atomic: only one caller can move an approval out of pending."""
        cursor = self._db.execute(
            "UPDATE approvals SET status = 'approved', source_message_id = ?, idempotency_key = ? WHERE id = ? AND status = 'pending'",
            (message_id, key, approval_id),
        )
        return cursor.rowcount == 1

    def set_approval_status(self, approval_id: str, status: str) -> None:
        self._db.execute("UPDATE approvals SET status = ? WHERE id = ?", (status, approval_id))

    def set_shown_reply(self, approval_id: str, reply_id: int) -> None:
        self._db.execute("UPDATE approvals SET shown_reply_id = ? WHERE id = ?", (reply_id, approval_id))

    def insert_attempt(self, attempt: Attempt) -> None:
        self._db.execute(
            """INSERT INTO attempts (id, approval_id, idempotency_key, hold_id, status, retries, returned_booking_id, contradiction_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (attempt.id, attempt.approval_id, attempt.idempotency_key, attempt.hold_id, attempt.status, attempt.retries,
             attempt.returned_booking_id, int(attempt.contradiction_seen)),
        )

    def save_attempt(self, attempt: Attempt) -> None:
        self._db.execute(
            "UPDATE attempts SET hold_id = ?, status = ?, retries = ?, returned_booking_id = ?, contradiction_seen = ? WHERE id = ?",
            (attempt.hold_id, attempt.status, attempt.retries, attempt.returned_booking_id, int(attempt.contradiction_seen), attempt.id),
        )

    def attempt_for_approval(self, approval_id: str) -> Attempt | None:
        row = self._db.execute("SELECT * FROM attempts WHERE approval_id = ?", (approval_id,)).fetchone()
        return None if row is None else self._to_attempt(row)

    def open_attempt(self, run_id: str) -> Attempt | None:
        row = self._db.execute(
            """SELECT attempts.* FROM attempts JOIN approvals ON attempts.approval_id = approvals.id
               WHERE approvals.run_id = ? AND attempts.status = 'open'""",
            (run_id,),
        ).fetchone()
        return None if row is None else self._to_attempt(row)

    # Prices.

    def save_booking_price(self, booking_id: str, price: int) -> None:
        self._db.execute("INSERT OR REPLACE INTO booking_prices (booking_id, price) VALUES (?, ?)", (booking_id, price))

    def booking_price(self, booking_id: str) -> int | None:
        row = self._db.execute("SELECT price FROM booking_prices WHERE booking_id = ?", (booking_id,)).fetchone()
        return None if row is None else row["price"]

    # Memory.

    def memory_fact(self, user_id: str, key: str) -> dict | None:
        row = self._db.execute("SELECT * FROM memory_facts WHERE user_id = ? AND key = ?", (user_id, key)).fetchone()
        return None if row is None else dict(row)

    def memory_facts(self, user_id: str) -> list[dict]:
        rows = self._db.execute("SELECT * FROM memory_facts WHERE user_id = ? ORDER BY key", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def save_memory_fact(self, user_id: str, key: str, value: str | None, provenance: str | None, status: str, evidence: str | None) -> None:
        self._db.execute(
            """INSERT INTO memory_facts (user_id, key, value, provenance, status, evidence, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value, provenance = excluded.provenance,
               status = excluded.status, evidence = excluded.evidence, updated_at = excluded.updated_at""",
            (user_id, key, value, provenance, status, evidence, self.clock.now().isoformat()),
        )

    # Row conversion.

    @staticmethod
    def _run_data(run: Run) -> str:
        data = asdict(run)
        for name in _RUN_FIELDS_OUTSIDE_DATA:
            del data[name]
        return json.dumps(data)

    @staticmethod
    def _load_run(row: sqlite3.Row) -> Run:
        return Run(id=row["id"], user_id=row["user_id"], state=State(row["state"]), **json.loads(row["data"]))

    @staticmethod
    def _to_approval(row: sqlite3.Row) -> Approval:
        fields = {k: row[k] for k in row.keys() if k != "hold_id"}
        return Approval(**fields)

    @staticmethod
    def _to_attempt(row: sqlite3.Row) -> Attempt:
        fields = dict(row)
        fields["contradiction_seen"] = bool(fields["contradiction_seen"])
        return Attempt(**fields)
