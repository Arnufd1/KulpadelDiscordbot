"""SQLite store for recurring weekly booking rules + booking history.

A `rule` is "every <weekday> at <HH:MM> Brussels time, book a padel slot."
The scheduler reads enabled rules and races to /sales at the slot-open moment.
"""
from __future__ import annotations
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day_of_week INTEGER NOT NULL,        -- 0=Mon ... 6=Sun
    time_local TEXT NOT NULL,            -- "HH:MM" Brussels
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER,
    attempted_at TEXT NOT NULL,
    success INTEGER NOT NULL,
    sale_id INTEGER,
    booking_id INTEGER,
    target_slot_iso TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reminded (
    -- tracks (booking_id, kind) pairs we already DM'd, to avoid duplicates
    booking_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (booking_id, kind)
);
"""


@dataclass
class Rule:
    id: int
    day_of_week: int
    time_local: str
    enabled: bool
    notes: str | None

    def label(self) -> str:
        return f"{WEEKDAY_NAMES[self.day_of_week]} {self.time_local}"


@dataclass
class HistoryRow:
    id: int
    rule_id: int | None
    attempted_at: str
    success: bool
    sale_id: int | None
    booking_id: int | None
    target_slot_iso: str | None
    error: str | None


class RulesStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.executescript(SCHEMA)

    # --- rules ---

    def add_rule(self, day_of_week: int, time_local: str, notes: str | None = None) -> Rule:
        if not (0 <= day_of_week <= 6):
            raise ValueError("day_of_week must be 0..6 (Mon..Sun)")
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO rules (day_of_week, time_local, enabled, created_at, notes) "
                "VALUES (?, ?, 1, ?, ?)",
                (day_of_week, time_local, datetime.utcnow().isoformat(), notes),
            )
            rule_id = cur.lastrowid
        return self.get_rule(rule_id)  # type: ignore[arg-type]

    def get_rule(self, rule_id: int) -> Rule | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
        return _row_to_rule(row) if row else None

    def list_rules(self, enabled_only: bool = False) -> list[Rule]:
        q = "SELECT * FROM rules" + (" WHERE enabled=1" if enabled_only else "")
        q += " ORDER BY day_of_week, time_local"
        with self._conn() as c:
            return [_row_to_rule(r) for r in c.execute(q).fetchall()]

    def remove_rule(self, rule_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM rules WHERE id=?", (rule_id,))
            return cur.rowcount > 0

    def set_enabled(self, rule_id: int, enabled: bool) -> bool:
        with self._conn() as c:
            cur = c.execute("UPDATE rules SET enabled=? WHERE id=?", (1 if enabled else 0, rule_id))
            return cur.rowcount > 0

    # --- history ---

    def record(
        self,
        *,
        rule_id: int | None,
        success: bool,
        sale_id: int | None,
        booking_id: int | None,
        target_slot_iso: str | None,
        error: str | None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO history (rule_id, attempted_at, success, sale_id, booking_id, target_slot_iso, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rule_id, datetime.utcnow().isoformat(), 1 if success else 0,
                 sale_id, booking_id, target_slot_iso, error),
            )

    # --- key/value (Discord state, etc.) ---

    def kv_get(self, key: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def kv_int(self, key: str) -> int | None:
        v = self.kv_get(key)
        try:
            return int(v) if v else None
        except ValueError:
            return None

    # --- reminders dedup ---

    def already_reminded(self, booking_id: int, kind: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM reminded WHERE booking_id=? AND kind=?",
                (booking_id, kind),
            ).fetchone()
        return row is not None

    def mark_reminded(self, booking_id: int, kind: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO reminded (booking_id, kind, sent_at) VALUES (?, ?, ?)",
                (booking_id, kind, datetime.utcnow().isoformat()),
            )

    def recent_history(self, limit: int = 10) -> list[HistoryRow]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_history(r) for r in rows]


def _row_to_rule(r: sqlite3.Row) -> Rule:
    return Rule(
        id=r["id"],
        day_of_week=r["day_of_week"],
        time_local=r["time_local"],
        enabled=bool(r["enabled"]),
        notes=r["notes"],
    )


def _row_to_history(r: sqlite3.Row) -> HistoryRow:
    return HistoryRow(
        id=r["id"],
        rule_id=r["rule_id"],
        attempted_at=r["attempted_at"],
        success=bool(r["success"]),
        sale_id=r["sale_id"],
        booking_id=r["booking_id"],
        target_slot_iso=r["target_slot_iso"],
        error=r["error"],
    )
