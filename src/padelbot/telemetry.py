"""Bounded, opt-in auth measurements. Never persist request data or credentials.

Only operation names, opaque platform identifiers, times and coarse outcomes
are accepted. A missing/unwritable database must never change an auth result.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from contextvars import ContextVar
from collections import deque
from datetime import datetime, timedelta, timezone
import math
import os
from pathlib import Path
import re
import sqlite3
import statistics
import threading
import time
import uuid
from zoneinfo import ZoneInfo


OPERATIONS = frozenset({"refresh_token", "silent_session", "android_login", "native_mfa", "emulator", "emulator_wait", "job", "job_wait"})
JOB_KINDS = frozenset({"slots", "quote", "book", "bookings", "cancel_booking", "login", "account", "rule", "community_join", "draft_refresh", "draft_release"})
CONTEXTS = frozenset({"login", "renewal", "enrollment", "probe"})
OUTCOMES = frozenset({"running", "success", "failed", "cancelled"})
ERRORS = frozenset({"session_expired", "network", "timeout", "configuration", "runtime", "interrupted", "unknown"})
RETENTION_DAYS = 90
MAX_ROWS = 200_000
_context: ContextVar[str | None] = ContextVar("auth_measurement_context", default=None)
_pruned: dict[str, float] = {}
_prune_lock = threading.Lock()


def _identifier(value: object) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32}", value) else None


def database_path() -> Path | None:
    value = os.environ.get("PADELWEB_TELEMETRY_DB", "")
    return Path(value).resolve() if value else None


def current_context() -> str:
    value = _context.get() or os.environ.get("PADELWEB_AUTH_CONTEXT", "login")
    return value if value in CONTEXTS else "login"


@contextmanager
def auth_context(value: str):
    token = _context.set(value if value in CONTEXTS else "login")
    try:
        yield
    finally:
        _context.reset(token)


def _connect(path: Path, *, create: bool = True) -> sqlite3.Connection:
    if create:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # WAL writers may briefly queue behind a filesystem flush. Bound the
        # wait without dropping ordinary concurrent measurements or retrying auth.
        c = sqlite3.connect(path, timeout=1.0)
    else:
        c = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.25)
    c.row_factory = sqlite3.Row
    if create:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
            CREATE TABLE IF NOT EXISTS auth_events (
                id TEXT PRIMARY KEY, user_id TEXT, job_id TEXT,
                operation TEXT NOT NULL, context TEXT NOT NULL,
                started REAL NOT NULL, finished REAL, duration_seconds REAL,
                outcome TEXT NOT NULL, error_category TEXT,
                native_used INTEGER NOT NULL DEFAULT 0, peak_rss_mb REAL, job_kind TEXT);
            CREATE INDEX IF NOT EXISTS auth_events_started ON auth_events(started);
            CREATE INDEX IF NOT EXISTS auth_events_profile ON auth_events(user_id,started);
        """)
        if "job_kind" not in {row[1] for row in c.execute("PRAGMA table_info(auth_events)")}:
            c.execute("ALTER TABLE auth_events ADD COLUMN job_kind TEXT")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        now = time.time()
        with _prune_lock:
            if now - _pruned.get(str(path), 0) > 3600:
                with c:
                    c.execute("DELETE FROM auth_events WHERE started < ?", (now - RETENTION_DAYS * 86400,))
                    # Bound growth even if there is a repeated provider outage.
                    c.execute("DELETE FROM auth_events WHERE id IN (SELECT id FROM auth_events ORDER BY started DESC LIMIT -1 OFFSET ?)", (MAX_ROWS,))
                _pruned[str(path)] = now
    return c


def error_category(error: BaseException) -> str:
    # Never store the exception message, traceback, provider response or URL.
    from .idp import SessionExpiredError
    if isinstance(error, SessionExpiredError):
        return "session_expired"
    if isinstance(error, TimeoutError) or type(error).__name__ in {"TimeoutExpired", "TimeoutException", "ConnectTimeout", "ReadTimeout", "PoolTimeout", "WriteTimeout"}:
        return "timeout"
    try:
        import httpx
        if isinstance(error, httpx.TransportError):
            return "network"
    except ImportError:
        pass
    if isinstance(error, (FileNotFoundError, ValueError)):
        return "configuration"
    if isinstance(error, RuntimeError):
        return "runtime"
    return "unknown"


class Measurement:
    def __init__(self, operation: str, *, context: str | None = None, job_kind: str | None = None):
        self.path = database_path()
        self.id = uuid.uuid4().hex
        self.operation = operation
        self.context = context if context in CONTEXTS else current_context()
        self.started = time.time()
        self.clock = time.perf_counter()
        self.native_used = False
        self.peak_rss_mb: float | None = None
        self._done = False
        if self.path is None or operation not in OPERATIONS:
            self.path = None
            return
        try:
            with closing(_connect(self.path)) as c, c:
                c.execute("INSERT INTO auth_events(id,user_id,job_id,operation,context,started,outcome,job_kind) VALUES(?,?,?,?,?,?,'running',?)",
                          (self.id, _identifier(os.environ.get("PADELWEB_USER_ID")), _identifier(os.environ.get("PADELWEB_JOB_ID")), operation, self.context, self.started, job_kind if job_kind in JOB_KINDS else None))
        except Exception:
            self.path = None

    def mark_native(self) -> None:
        self.native_used = True

    def finish(self, error: BaseException | None = None, *, cancelled: bool = False) -> None:
        if self._done:
            return
        self._done = True
        if self.path is None:
            return
        try:
            duration = max(0.0, time.perf_counter() - self.clock)
            with closing(_connect(self.path)) as c, c:
                c.execute("UPDATE auth_events SET finished=?,duration_seconds=?,outcome=?,error_category=?,native_used=?,peak_rss_mb=? WHERE id=?",
                          (time.time(), duration, "cancelled" if cancelled else "failed" if error else "success",
                           "interrupted" if cancelled else error_category(error) if error else None,
                           int(self.native_used), self.peak_rss_mb, self.id))
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, kind, error, tb):
        self.finish(error, cancelled=kind is not None and not issubclass(kind, Exception))


def measure(operation: str, *, context: str | None = None, job_kind: str | None = None) -> Measurement:
    return Measurement(operation, context=context, job_kind=job_kind)


class ProcessMemorySampler:
    """One cheap /proc read per second, only while an owned emulator is alive."""
    def __init__(self, pid: int, measurement: Measurement):
        self.path = Path(f"/proc/{pid}/status")
        self.measurement = measurement
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        if measurement.path is not None and os.name == "posix" and self.path.is_file():
            self.thread = threading.Thread(target=self._sample, name="auth-memory", daemon=True)
            self.thread.start()

    def _sample(self):
        while not self.stop.is_set():
            try:
                values = dict(line.split(":", 1) for line in self.path.read_text().splitlines() if ":" in line)
                # VmHWM covers peaks between samples and includes the QEMU process
                # address space; this is not total host or cgroup memory usage.
                rss = max(float(values.get(name, "0 kB").split()[0]) for name in ("VmHWM", "VmRSS")) / 1024
                self.measurement.peak_rss_mb = max(self.measurement.peak_rss_mb or 0, rss)
            except (OSError, ValueError, IndexError):
                break
            self.stop.wait(1)

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=0.1)


def _cadence(rows: list[dict]) -> dict:
    # Intervals belong to one profile. Combining different users creates fake
    # short intervals when several people naturally reauthenticate together.
    groups: dict[str, list[float]] = {}
    for row in rows:
        if row["operation"] == "android_login" and row["native_used"] and row["outcome"] == "success" and row["context"] in {"renewal", "login"} and row["user_id"]:
            groups.setdefault(row["user_id"], []).append(row["finished"])
    return _cadence_from_times(groups)


def _cadence_from_times(groups: dict[str, list[float]]) -> dict:
    intervals = [b - a for values in groups.values() for a, b in zip(sorted(values), sorted(values)[1:]) if b > a]
    return {"sample_count": len(intervals), "median_seconds": statistics.median(intervals) if intervals else None,
            "min_seconds": min(intervals) if intervals else None, "max_seconds": max(intervals) if intervals else None,
            "last_success_at": max((at for values in groups.values() for at in values), default=None)}


def _totals(rows: list[dict]) -> dict:
    ordinary = [r for r in rows if r["context"] != "probe"]
    native = [r for r in ordinary if r["operation"] == "emulator"]
    mfa = [r for r in ordinary if r["operation"] == "android_login" and r["native_used"] and r["context"] != "enrollment"]
    def count(operation, outcome):
        return sum(r["operation"] == operation and r["outcome"] == outcome for r in ordinary)
    return {"emulator_starts": len(native), "emulator_completed": sum(r["outcome"] == "success" for r in native),
            "emulator_failures": sum(r["outcome"] in {"failed", "cancelled"} for r in native),
            "enrollment_starts": sum(r["context"] == "enrollment" for r in native),
            "reauth_starts": sum(r["context"] in {"login", "renewal"} for r in native),
            "mfa_successes": sum(r["outcome"] == "success" for r in mfa),
            "mfa_failures": sum(r["outcome"] in {"failed", "cancelled"} for r in mfa),
            "silent_successes": count("silent_session", "success") + sum(r["operation"] == "android_login" and not r["native_used"] and r["outcome"] == "success" for r in ordinary),
            "silent_failures": count("silent_session", "failed"),
            "token_refresh_successes": count("refresh_token", "success"), "token_refresh_failures": count("refresh_token", "failed"),
            "native_seconds": sum(r["duration_seconds"] or 0 for r in native),
            "queue_wait_seconds": sum(r["duration_seconds"] or 0 for r in ordinary if r["operation"] == "emulator_wait"),
            "jobs_completed": sum(r["operation"] == "job" and r["outcome"] == "success" for r in ordinary),
            "jobs_failed": sum(r["operation"] == "job" and r["outcome"] in {"failed", "cancelled"} for r in ordinary),
            "job_wait_seconds": sum(r["duration_seconds"] or 0 for r in ordinary if r["operation"] == "job_wait")}


def _add_totals(totals: dict, row: dict) -> None:
    if row["context"] == "probe":
        return
    operation, outcome = row["operation"], row["outcome"]
    success = outcome == "success"
    failed = outcome in {"failed", "cancelled"}
    duration = row["duration_seconds"] or 0
    if operation == "emulator":
        totals["emulator_starts"] += 1
        totals["emulator_completed"] += success
        totals["emulator_failures"] += failed
        totals["enrollment_starts"] += row["context"] == "enrollment"
        totals["reauth_starts"] += row["context"] in {"login", "renewal"}
        totals["native_seconds"] += duration
    elif operation == "android_login":
        if row["native_used"] and row["context"] != "enrollment":
            totals["mfa_successes"] += success
            totals["mfa_failures"] += failed
        elif not row["native_used"]:
            totals["silent_successes"] += success
    elif operation == "silent_session":
        totals["silent_successes"] += success
        totals["silent_failures"] += outcome == "failed"
    elif operation == "refresh_token":
        totals["token_refresh_successes"] += success
        totals["token_refresh_failures"] += outcome == "failed"
    elif operation == "emulator_wait":
        totals["queue_wait_seconds"] += duration
    elif operation == "job":
        totals["jobs_completed"] += success
        totals["jobs_failed"] += failed
    elif operation == "job_wait":
        totals["job_wait_seconds"] += duration


def metrics_summary(path: Path | str | None = None, *, user_id: str | None = None, days: int = 30) -> dict:
    """Admin-only caller must enforce owner/Tailscale authorization itself."""
    path = Path(path) if path else database_path()
    days = max(1, min(RETENTION_DAYS, int(days)))
    now = time.time()
    result = {"enabled": path is not None, "available": False,
              "window": {"days": days, "since": now - days * 86400, "until": now, "observed_since": None, "observed_seconds": 0},
              "totals": _totals([]), "cadence": _cadence([]), "profiles": [], "recent": [], "daily": [],
              "performance": {"mean_native_seconds": None, "p95_native_seconds": None, "mean_queue_wait_seconds": None, "mean_job_seconds": None, "mean_job_wait_seconds": None, "peak_rss_mb": None}}
    if path is None or not path.is_file():
        return result
    totals = _totals([])
    profiles: dict[str, dict] = {}
    cadence_times: dict[str, list[float]] = {}
    reporting_timezone = ZoneInfo("Europe/Brussels")
    today = datetime.fromtimestamp(now, reporting_timezone).date()
    daily = {(today - timedelta(days=offset)).isoformat(): _totals([]) for offset in reversed(range(min(days, 7)))}
    recent: deque[dict] = deque(maxlen=50)
    native_durations: list[float] = []
    measures = {name: [0.0, 0] for name in ("emulator_wait", "job", "job_wait")}
    peak_rss = None
    first = None
    try:
        with closing(_connect(path, create=False)) as c:
            where = "started>=?"
            args: tuple = (result["window"]["since"],)
            if user_id is not None:
                where += " AND user_id=?"
                args += (_identifier(user_id) or "invalid",)
            # Stream the event table: the small app VM must not allocate a
            # dictionary for every historic job just to render one admin chart.
            for record in c.execute("SELECT * FROM auth_events WHERE " + where + " ORDER BY started", args):
                row = dict(record)
                recent.append({k: v for k, v in row.items() if k != "job_id"})
                if first is None:
                    first = row["started"]
                _add_totals(totals, row)
                date = datetime.fromtimestamp(row["started"], reporting_timezone).date().isoformat()
                if date in daily:
                    _add_totals(daily[date], row)
                profile_id = row["user_id"]
                if profile_id:
                    if profile_id not in profiles:
                        profiles[profile_id] = {"user_id": profile_id, **_totals([]), "last_event_at": row["started"]}
                    profile = profiles[profile_id]
                    _add_totals(profile, row)
                    profile["last_event_at"] = row["started"]
                if row["operation"] == "android_login" and row["native_used"] and row["outcome"] == "success" and row["context"] in {"login", "renewal"} and profile_id:
                    cadence_times.setdefault(profile_id, []).append(row["finished"])
                if row["context"] == "probe" or row["duration_seconds"] is None:
                    continue
                if row["operation"] == "emulator":
                    native_durations.append(row["duration_seconds"])
                    if row["peak_rss_mb"] is not None:
                        peak_rss = max(peak_rss or 0, row["peak_rss_mb"])
                elif row["operation"] in measures:
                    value = measures[row["operation"]]
                    value[0] += row["duration_seconds"]
                    value[1] += 1
        result["available"] = True
    except (OSError, sqlite3.Error):
        return result
    result["totals"] = totals
    result["cadence"] = _cadence_from_times(cadence_times)
    if first is not None:
        result["window"].update(observed_since=first, observed_seconds=max(0, now - first))
    native_durations.sort()
    def mean(operation):
        total, count = measures[operation]
        return total / count if count else None
    result["performance"] = {"mean_native_seconds": statistics.mean(native_durations) if native_durations else None,
                             "p95_native_seconds": native_durations[max(0, math.ceil(len(native_durations) * .95) - 1)] if native_durations else None,
                             "mean_queue_wait_seconds": mean("emulator_wait"), "mean_job_seconds": mean("job"),
                             "mean_job_wait_seconds": mean("job_wait"), "peak_rss_mb": peak_rss}
    result["profiles"] = [{**profiles[profile], "cadence": _cadence_from_times({profile: cadence_times.get(profile, [])})} for profile in sorted(profiles)]
    result["daily"] = [{"date": date, **{name: values[name] for name in ("emulator_starts", "mfa_successes", "silent_successes", "native_seconds")}} for date, values in daily.items()]
    result["recent"] = list(reversed(recent))
    return result


_EVENT_FIELDS = ("id", "user_id", "job_id", "operation", "context", "started", "finished", "duration_seconds", "outcome", "error_category", "native_used", "peak_rss_mb", "job_kind")


def export_events(path: Path | str | None = None, *, job_id: str | None = None, since: float | None = None, limit: int = 5000) -> list[dict]:
    """Copy only the fixed metadata schema through the authenticated VM channel."""
    path = Path(path) if path else database_path()
    if path is None or not path.is_file():
        return []
    try:
        where = ["started>=?"]
        values: list = [max(time.time() - RETENTION_DAYS * 86400, float(since or 0))]
        if job_id is not None:
            where.append("job_id=?")
            values.append(_identifier(job_id) or "invalid")
        values.append(max(1, min(5000, int(limit))))
        with closing(_connect(path, create=False)) as c:
            return [{name: row[name] for name in _EVENT_FIELDS} for row in c.execute(
                "SELECT * FROM auth_events WHERE " + " AND ".join(where) + " ORDER BY started DESC LIMIT ?", values)]
    except Exception:
        return []


def _validated_event(row: object, now: float) -> tuple | None:
    if not isinstance(row, dict) or set(row) != set(_EVENT_FIELDS):
        return None
    if not _identifier(row["id"]) or row["operation"] not in OPERATIONS or row["context"] not in CONTEXTS or row["outcome"] not in OUTCOMES:
        return None
    if any(row[name] is not None and _identifier(row[name]) is None for name in ("user_id", "job_id")):
        return None
    if row["error_category"] is not None and row["error_category"] not in ERRORS:
        return None
    if row["job_kind"] is not None and row["job_kind"] not in JOB_KINDS:
        return None
    if row["native_used"] not in (0, 1):
        return None
    for name in ("started", "finished", "duration_seconds", "peak_rss_mb"):
        value = row[name]
        if value is not None and (type(value) not in {float, int} or not math.isfinite(value)):
            return None
    started, finished, duration = row["started"], row["finished"], row["duration_seconds"]
    if started is None or not now - RETENTION_DAYS * 86400 <= started <= now + 300:
        return None
    if row["outcome"] == "running":
        if finished is not None or duration is not None:
            return None
    elif finished is None or duration is None or not started <= finished <= now + 300 or not 0 <= duration <= 172800:
        return None
    if row["peak_rss_mb"] is not None and not 0 <= row["peak_rss_mb"] <= 1_048_576:
        return None
    return tuple(row[name] for name in _EVENT_FIELDS)


def ingest_events(path: Path | str | None, events: list[dict]) -> int:
    """Idempotent, bounded metadata import; finished events cannot be replayed away."""
    path = Path(path) if path else database_path()
    if path is None or not isinstance(events, list) or len(events) > 5000:
        return 0
    try:
        now = time.time()
        rows = [value for row in events if (value := _validated_event(row, now)) is not None]
        if not rows:
            return 0
        with closing(_connect(path)) as c, c:
            before = c.total_changes
            c.executemany("""INSERT INTO auth_events(id,user_id,job_id,operation,context,started,finished,duration_seconds,outcome,error_category,native_used,peak_rss_mb,job_kind)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                finished=excluded.finished,duration_seconds=excluded.duration_seconds,outcome=excluded.outcome,
                error_category=excluded.error_category,native_used=excluded.native_used,peak_rss_mb=excluded.peak_rss_mb
                WHERE auth_events.outcome='running' AND excluded.finished IS NOT NULL
                AND auth_events.user_id IS excluded.user_id AND auth_events.job_id IS excluded.job_id
                AND auth_events.operation=excluded.operation AND auth_events.context=excluded.context
                AND auth_events.started=excluded.started""", rows)
            return c.total_changes - before
    except Exception:
        return 0


def record_job(path: Path | str | None, *, user_id: str, job_id: str, kind: str,
               created: float, started: float, finished: float, status: str) -> int:
    """Parent dispatcher records queue delay without mutating process-wide env."""
    if status not in {"done", "failed", "cancelled", "unknown"} or kind not in JOB_KINDS or not _identifier(job_id):
        return 0
    outcome = "success" if status == "done" else "cancelled" if status == "cancelled" else "failed"
    rows = []
    for operation, first, last, result in (("job", started, finished, outcome), ("job_wait", created, started, "success")):
        rows.append({"id": uuid.uuid5(uuid.NAMESPACE_OID, job_id + ":" + operation).hex,
                     "user_id": user_id, "job_id": job_id, "operation": operation,
                     "context": "login" if kind == "login" else "renewal", "started": first, "finished": last,
                     "duration_seconds": max(0, last - first), "outcome": result,
                     "error_category": "interrupted" if status in {"cancelled", "unknown"} and operation == "job" else "unknown" if result == "failed" else None,
                     "native_used": 0, "peak_rss_mb": None, "job_kind": kind})
    return ingest_events(path, rows)
