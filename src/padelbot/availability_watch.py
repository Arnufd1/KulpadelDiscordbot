"""Persistent one-day notification watches, separate from automatic booking rules."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from loguru import logger

from .booking import find_slots_window, get_product_names
from .client import BackboneClient
from .config import settings
from . import weather


def local_now():
    return datetime.now(ZoneInfo(settings.padel_local_tz))


@dataclass(frozen=True)
class DayWatch:
    id: int
    owner_id: int
    day: date
    from_hour: int
    to_hour: int
    active: bool
    snapshot: dict[str, list[int]] | None
    checked_at: str | None
    error: str | None
    visibility: str = "private"
    public_channel_id: int | None = None
    public_message_id: int | None = None
    public_guild_id: int | None = None
    public_live: bool = False
    public_pending: bool = False
    public_alert_attempt: str | None = None
    origin_channel_id: int | None = None
    origin_guild_id: int | None = None

    @property
    def chat_url(self):
        if self.origin_channel_id:
            return f"https://discord.com/channels/{self.origin_guild_id or '@me'}/{self.origin_channel_id}"
        return None

    @property
    def jump_url(self):
        if self.public_channel_id and self.public_message_id:
            return f"https://discord.com/channels/{self.public_guild_id or '@me'}/{self.public_channel_id}/{self.public_message_id}"
        return None

    @property
    def ends_at(self):
        return datetime.combine(self.day, datetime.min.time(), ZoneInfo(settings.padel_local_tz)) + timedelta(hours=self.to_hour)

    def label(self):
        return f"{self.day:%d/%m} · {self.from_hour:02}:00–{self.to_hour:02}:00"


class WatchStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS availability_watches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    from_hour INTEGER NOT NULL,
                    to_hour INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    snapshot TEXT,
                    checked_at TEXT,
                    error TEXT
                );
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(availability_watches)")}
            for name, definition in {
                "visibility": "TEXT NOT NULL DEFAULT 'private'",
                "public_channel_id": "INTEGER", "public_message_id": "INTEGER", "public_guild_id": "INTEGER",
                "public_live": "INTEGER NOT NULL DEFAULT 0", "public_pending": "INTEGER NOT NULL DEFAULT 0",
                "public_alert_attempt": "TEXT",
                "origin_channel_id": "INTEGER", "origin_guild_id": "INTEGER",
            }.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE availability_watches ADD COLUMN {name} {definition}")
            # Public watches already have a known source; older private watches
            # are attached when their owner next opens them in a chat.
            conn.execute("UPDATE availability_watches SET origin_channel_id=public_channel_id,origin_guild_id=public_guild_id WHERE origin_channel_id IS NULL AND public_channel_id IS NOT NULL")
            conn.execute("DROP INDEX IF EXISTS unique_active_watch")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS unique_active_watch_per_chat ON availability_watches(owner_id,day,from_hour,to_hour,COALESCE(origin_channel_id,0)) WHERE active=1")

    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row(row):
        if row is None:
            return None
        return DayWatch(row["id"], row["owner_id"], date.fromisoformat(row["day"]), row["from_hour"],
            row["to_hour"], bool(row["active"]), json.loads(row["snapshot"]) if row["snapshot"] is not None else None,
            row["checked_at"], row["error"], row["visibility"], row["public_channel_id"], row["public_message_id"],
            row["public_guild_id"], bool(row["public_live"]), bool(row["public_pending"]), row["public_alert_attempt"],
            row["origin_channel_id"], row["origin_guild_id"])

    def get(self, watch_id):
        with self._conn() as conn:
            return self._row(conn.execute("SELECT * FROM availability_watches WHERE id=?", (watch_id,)).fetchone())

    def list_active(self, owner_id=None):
        query = "SELECT * FROM availability_watches WHERE active=1"
        params = ()
        if owner_id is not None:
            query += " AND owner_id=?"
            params = (owner_id,)
        with self._conn() as conn:
            return [self._row(row) for row in conn.execute(query + " ORDER BY day,from_hour,id", params)]

    def create(self, owner_id, day, from_hour, to_hour, *, channel_id=None, guild_id=None, now=None):
        now = now or local_now()
        if not owner_id or not 0 <= from_hour < to_hour <= 24:
            raise ValueError("Kies een begin- en einduur tussen 0 en 24; het einduur moet later zijn.")
        if day < now.date() or day > now.date() + timedelta(days=365):
            raise ValueError("Kies een dag van vandaag tot maximaal 365 dagen vooruit.")
        ends_at = datetime.combine(day, datetime.min.time(), now.tzinfo) + timedelta(hours=to_hour)
        if ends_at <= now:
            raise ValueError("Dit tijdsvenster is al voorbij. Kies een later uur of een andere dag.")
        with self._conn() as conn:
            existing = conn.execute("SELECT id FROM availability_watches WHERE active=1 AND owner_id=? AND day=? AND from_hour=? AND to_hour=? AND (origin_channel_id IS ? OR origin_channel_id IS NULL) ORDER BY origin_channel_id IS NULL LIMIT 1",
                (owner_id, day.isoformat(), from_hour, to_hour, channel_id)).fetchone()
            if existing:
                if channel_id is not None:
                    conn.execute("UPDATE availability_watches SET origin_channel_id=?,origin_guild_id=? WHERE id=? AND origin_channel_id IS NULL",
                        (channel_id, guild_id, existing["id"]))
                    conn.commit()
                return self.get(existing["id"]), False
            count = conn.execute("SELECT COUNT(*) FROM availability_watches WHERE active=1 AND owner_id=?", (owner_id,)).fetchone()[0]
            if count >= 20:
                raise ValueError("Je hebt al 20 actieve watchers. Stop eerst een watcher.")
            cursor = conn.execute("INSERT INTO availability_watches(owner_id,day,from_hour,to_hour,origin_channel_id,origin_guild_id) VALUES (?,?,?,?,?,?)",
                (owner_id, day.isoformat(), from_hour, to_hour, channel_id, guild_id))
            watch_id = cursor.lastrowid
        return self.get(watch_id), True

    def bind_origin(self, watch_id, owner_id, channel_id, guild_id):
        if channel_id is None:
            return False
        with self._conn() as conn:
            cursor = conn.execute("UPDATE availability_watches SET origin_channel_id=?,origin_guild_id=? WHERE id=? AND owner_id=? AND origin_channel_id IS NULL",
                (channel_id, guild_id, watch_id, owner_id))
            return cursor.rowcount > 0

    def stop(self, watch_id, owner_id):
        with self._conn() as conn:
            cursor = conn.execute("UPDATE availability_watches SET active=0 WHERE id=? AND owner_id=? AND active=1", (watch_id, owner_id))
            return cursor.rowcount > 0

    def save_check(self, watch_id, snapshot, *, now=None, error=None):
        stamp = (now or local_now()).isoformat()
        with self._conn() as conn:
            if error is not None:
                conn.execute("UPDATE availability_watches SET error=? WHERE id=? AND active=1", (error, watch_id))
            else:
                conn.execute("UPDATE availability_watches SET snapshot=?,checked_at=?,error=NULL WHERE id=? AND active=1",
                    (json.dumps(snapshot, sort_keys=True), stamp, watch_id))

    def begin_publish(self, watch_id, owner_id):
        with self._conn() as conn:
            cursor = conn.execute("UPDATE availability_watches SET public_pending=1 WHERE id=? AND owner_id=? AND active=1 AND visibility='private' AND public_pending=0",
                (watch_id, owner_id))
            return cursor.rowcount > 0

    def finish_publish(self, watch_id, owner_id, *, channel_id, message_id, guild_id, live):
        with self._conn() as conn:
            cursor = conn.execute("UPDATE availability_watches SET visibility='public',public_channel_id=?,public_message_id=?,public_guild_id=?,public_live=?,public_pending=0,public_alert_attempt=NULL,origin_channel_id=COALESCE(origin_channel_id,?),origin_guild_id=COALESCE(origin_guild_id,?) WHERE id=? AND owner_id=? AND public_pending=1 AND (origin_channel_id IS NULL OR origin_channel_id=?)",
                (channel_id, message_id, guild_id, int(live), channel_id, guild_id, watch_id, owner_id, channel_id))
            return cursor.rowcount > 0

    def make_private(self, watch_id, owner_id):
        # Keep the previous anchor so it can still be redacted by a fresh click.
        with self._conn() as conn:
            cursor = conn.execute("UPDATE availability_watches SET visibility='private',public_live=0,public_pending=0,public_alert_attempt=NULL WHERE id=? AND owner_id=?",
                (watch_id, owner_id))
            return cursor.rowcount > 0

    def publication_failed(self, watch_id, owner_id):
        with self._conn() as conn:
            conn.execute("UPDATE availability_watches SET public_pending=0 WHERE id=? AND owner_id=? AND visibility='private'", (watch_id, owner_id))

    def mark_public_alert(self, watch_id, owner_id, fingerprint):
        with self._conn() as conn:
            conn.execute("UPDATE availability_watches SET public_alert_attempt=? WHERE id=? AND owner_id=? AND active=1 AND visibility='public'",
                (fingerprint, watch_id, owner_id))

    def expire(self, *, now=None):
        now = now or local_now()
        for watch in self.list_active():
            if watch.ends_at <= now:
                self.stop(watch.id, watch.owner_id)


def filter_slots(watch: DayWatch, slots, *, now=None):
    now = now or local_now()
    return [slot for slot in slots
        if (start := slot.start_dt.astimezone(now.tzinfo)).date() == watch.day
        and start > now
        and watch.from_hour * 60 <= start.hour * 60 + start.minute <= watch.to_hour * 60]


def snapshot_slots(watch, slots, *, now=None):
    times = defaultdict(set)
    for slot in filter_slots(watch, slots, now=now):
        stamp = slot.start_dt.astimezone(ZoneInfo(settings.padel_local_tz)).strftime("%H:%M")
        times[stamp].add(slot.bookable_product_id)
    return {stamp: sorted(courts) for stamp, courts in sorted(times.items())}


def changes(watch, snapshot, *, now=None):
    now = now or local_now()
    previous = watch.snapshot or {}
    opened = sorted(set(snapshot) - set(previous))
    # A passed start time is not a newly occupied time.
    closed = sorted(stamp for stamp in set(previous) - set(snapshot)
        if datetime.combine(watch.day, datetime.strptime(stamp, "%H:%M").time(), now.tzinfo) > now)
    return opened, closed


def fetch_watch_day(day):
    target = datetime.combine(day, datetime.min.time(), ZoneInfo(settings.padel_local_tz))
    if day > local_now().date() + timedelta(days=settings.padel_opens_days_ahead):
        return [], {}
    with BackboneClient() as client:
        slots = find_slots_window(client, start_date=target, days=1)
        names = get_product_names(client, {slot.bookable_product_id for slot in slots}) if slots else {}
    return slots, names


async def poll_watches(bot):
    """One shared read per day. Failed reads never mean 'fully booked'."""
    async with bot._availability_watch_lock:
        store = bot.watch_store
        store.expire()
        active = store.list_active(settings.discord_owner_id)
        sessions = getattr(bot, "_watch_interactions", {})
        active_ids = {watch.id for watch in active}
        for watch_id in list(sessions):
            if watch_id not in active_ids:
                sessions.pop(watch_id, None)
        by_day = defaultdict(list)
        for watch in active:
            if watch.day <= local_now().date() + timedelta(days=settings.padel_opens_days_ahead):
                by_day[watch.day].append(watch)
        for day, watches in by_day.items():
            try:
                slots, names = await asyncio.to_thread(fetch_watch_day, day)
            except Exception as error:
                logger.warning("Availability watch read failed: {}", type(error).__name__)
                for watch in watches:
                    store.save_check(watch.id, None, error="KUL tijdelijk onbereikbaar; vorige beschikbaarheid bewaard.")
                continue
            forecast = await asyncio.to_thread(weather.get_forecast)
            for watch in watches:
                current = store.get(watch.id)
                if current is None or not current.active:
                    continue
                now = local_now()
                if current.ends_at <= now:
                    store.stop(current.id, current.owner_id)
                    continue
                snapshot = snapshot_slots(current, slots, now=now)
                opened, closed = changes(current, snapshot, now=now)
                if opened or closed:
                    from .watch_ui import deliver_notification, WatchDeliveryUnavailable
                    try:
                        notice = await deliver_notification(bot, current, snapshot, names, opened, closed, forecast)
                    except WatchDeliveryUnavailable as error:
                        store.save_check(current.id, None, error=str(error))
                        continue
                    except Exception as error:
                        logger.warning("Availability watch notification failed: {}", type(error).__name__)
                        store.save_check(current.id, None, error="Melding niet verstuurd in de oorspronkelijke chat. Controleer de kanaalrechten of vernieuw daar de watcher; we proberen opnieuw.")
                        continue
                else:
                    notice = None
                store.save_check(current.id, snapshot, now=now)
                if notice:
                    store.save_check(current.id, None, error=notice)


async def run_availability_watcher(bot):
    logger.info("Availability notifications started (poll {}s).", settings.padelbot_watcher_poll_s)
    while True:
        try:
            await poll_watches(bot)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Availability notification loop failed: {}", type(error).__name__)
        await asyncio.sleep(max(30, settings.padelbot_watcher_poll_s))
