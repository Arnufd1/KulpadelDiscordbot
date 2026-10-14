"""Persistent Discord player lists, separate from KU Leuven purchases."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import sqlite3
import time
import uuid


@dataclass(frozen=True)
class Match:
    id: str
    owner_id: int | str
    booking_id: int
    start_iso: str
    end_iso: str
    court: str
    channel_id: int
    guild_id: int | None
    message_id: int | None
    status: str
    players: tuple[int | str, ...]
    capacity: int = 4
    names: dict = field(default_factory=dict)
    hub_profile_id: str | None = None
    hub_verified: float = 0
    hub_confirmed: bool = False
    hub_visible: bool = True

    @property
    def started(self) -> bool:
        return datetime.fromisoformat(self.start_iso.replace("Z", "+00:00")).timestamp() <= time.time()

    @property
    def jump_url(self) -> str | None:
        if self.message_id is None:
            return None
        return f"https://discord.com/channels/{self.guild_id or '@me'}/{self.channel_id}/{self.message_id}"


class MatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class MatchPost:
    id: str
    match_id: str
    channel_id: int
    guild_id: int | None
    message_id: int | None
    state: str
    replacement_id: str | None = None
    move_requested: bool = False

    @property
    def jump_url(self):
        return f"https://discord.com/channels/{self.guild_id or '@me'}/{self.channel_id}/{self.message_id}" if self.message_id is not None else None


class MatchStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS shared_matches (
                    id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL,
                    booking_id INTEGER NOT NULL, start_iso TEXT NOT NULL,
                    end_iso TEXT NOT NULL, start_at REAL NOT NULL,
                    court TEXT NOT NULL, channel_id INTEGER NOT NULL,
                    guild_id INTEGER, message_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'open',
                    UNIQUE(owner_id, booking_id)
                );
                CREATE TABLE IF NOT EXISTS match_players (
                    match_id TEXT NOT NULL REFERENCES shared_matches(id),
                    user_id INTEGER NOT NULL, joined_at REAL NOT NULL,
                    PRIMARY KEY(match_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS shared_match_posts (
                    id TEXT PRIMARY KEY,
                    match_id TEXT NOT NULL REFERENCES shared_matches(id),
                    channel_id INTEGER NOT NULL, guild_id INTEGER, message_id INTEGER,
                    state TEXT NOT NULL DEFAULT 'active', replacement_id TEXT,
                    move_requested INTEGER NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS unique_active_match_channel
                    ON shared_match_posts(match_id,channel_id) WHERE state='active';
                CREATE UNIQUE INDEX IF NOT EXISTS unique_match_message
                    ON shared_match_posts(channel_id,message_id) WHERE message_id IS NOT NULL;
                INSERT OR IGNORE INTO shared_match_posts(id,match_id,channel_id,guild_id,message_id)
                    SELECT id,id,channel_id,guild_id,message_id FROM shared_matches WHERE channel_id>0;
                CREATE TABLE IF NOT EXISTS match_notices (
                    channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                    match_id TEXT NOT NULL REFERENCES shared_matches(id),
                    due_at REAL NOT NULL, retry_at REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(channel_id,message_id)
                );
                CREATE TABLE IF NOT EXISTS hub_profiles (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, discord_id INTEGER UNIQUE,
                    suspended INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS hub_discord_names (id INTEGER PRIMARY KEY,name TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS hub_link_codes (
                    code TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES hub_profiles(id),expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS hub_post_updates (
                    match_id TEXT PRIMARY KEY REFERENCES shared_matches(id),revision INTEGER NOT NULL DEFAULT 1,
                    retry_at REAL NOT NULL DEFAULT 0);
            """)
            columns={r['name'] for r in c.execute('PRAGMA table_info(shared_matches)')}
            for name,definition in {'hub_profile_id':'TEXT','hub_verified':'REAL NOT NULL DEFAULT 0',
                                    'hub_confirmed':'INTEGER NOT NULL DEFAULT 0','hub_visible':'INTEGER NOT NULL DEFAULT 1'}.items():
                if name not in columns:c.execute(f'ALTER TABLE shared_matches ADD COLUMN {name} {definition}')

    @contextmanager
    def _connection(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            with c:
                yield c
        finally:
            c.close()

    def _get(self, c, match_id: str) -> Match | None:
        row = c.execute("SELECT * FROM shared_matches WHERE id=?", (match_id,)).fetchone()
        if row is None:
            return None
        players = tuple(r[0] for r in c.execute(
            "SELECT user_id FROM match_players WHERE match_id=? ORDER BY joined_at, user_id", (match_id,)))
        names={}
        for actor in set(players+(row['owner_id'],)):
            if isinstance(actor,int):
                person=c.execute('SELECT name FROM hub_profiles WHERE discord_id=?',(actor,)).fetchone()
                person=person or c.execute('SELECT name FROM hub_discord_names WHERE id=?',(actor,)).fetchone()
            else:
                person=c.execute('SELECT name FROM hub_profiles WHERE id=?',(str(actor).removeprefix('web:'),)).fetchone()
            names[actor]=person['name'] if person else 'Discord-speler' if isinstance(actor,int) else 'Speler'
        return Match(row["id"], row["owner_id"], row["booking_id"], row["start_iso"], row["end_iso"],
                     row["court"], row["channel_id"], row["guild_id"], row["message_id"], row["status"], players,
                     names=names,hub_profile_id=row['hub_profile_id'],hub_verified=row['hub_verified'],
                     hub_confirmed=bool(row['hub_confirmed']),hub_visible=bool(row['hub_visible']))

    def get(self, match_id: str) -> Match | None:
        with self._connection() as c:
            return self._get(c, match_id)

    def for_booking(self, owner_id: int, booking_id: int) -> Match | None:
        with self._connection() as c:
            row = c.execute("SELECT id FROM shared_matches WHERE owner_id=? AND booking_id=?",
                            (owner_id, booking_id)).fetchone()
            return self._get(c, row[0]) if row else None

    def create(self, *, owner_id, booking_id, start_iso, end_iso, court, channel_id, guild_id) -> Match:
        start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        if start.tzinfo is None or end.tzinfo is None or end <= start or start.timestamp() <= time.time():
            raise MatchError("Deze reservatie is niet meer beschikbaar om te delen.")
        match_id = uuid.uuid4().hex
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT id FROM shared_matches WHERE owner_id=? AND booking_id=?",
                            (owner_id, booking_id)).fetchone()
            if row:
                return self._get(c, row[0])
            if c.execute("SELECT 1 FROM shared_matches WHERE booking_id=? AND status='open'",(booking_id,)).fetchone():
                raise MatchError('Deze reservatie staat al in de hub. Koppel eerst je Discord-account aan je profiel.')
            c.execute("INSERT INTO shared_matches (id,owner_id,booking_id,start_iso,end_iso,start_at,court,channel_id,guild_id) VALUES (?,?,?,?,?,?,?,?,?)",
                      (match_id, owner_id, booking_id, start_iso, end_iso, start.timestamp(), court, channel_id or 0, guild_id))
            c.execute("INSERT INTO match_players VALUES (?,?,?)", (match_id, owner_id, time.time()))
            profile=c.execute('SELECT id FROM hub_profiles WHERE discord_id=?',(owner_id,)).fetchone()
            c.execute('UPDATE shared_matches SET hub_verified=?,hub_confirmed=1,hub_profile_id=? WHERE id=?',
                      (time.time(),profile['id'] if profile else None,match_id))
            if channel_id:
                c.execute("INSERT INTO shared_match_posts(id,match_id,channel_id,guild_id) VALUES (?,?,?,?)",
                    (match_id, match_id, channel_id, guild_id))
            return self._get(c, match_id)

    def bind_message(self, match_id: str, channel_id: int, message_id: int) -> Match:
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            match = self._get(c, match_id)
            if match is None or match.channel_id != channel_id or match.message_id not in (None, message_id):
                raise MatchError("Deze knop hoort niet bij het gedeelde groepsbericht.")
            c.execute("UPDATE shared_matches SET message_id=? WHERE id=?", (message_id, match_id))
            c.execute("UPDATE shared_match_posts SET message_id=? WHERE id=?", (message_id, match_id))
            return self._get(c, match_id)

    def drop_unpublished(self, match_id):
        with self._connection() as c:
            row = c.execute("SELECT message_id FROM shared_matches WHERE id=?", (match_id,)).fetchone()
            if row and row[0] is None:
                c.execute("DELETE FROM shared_match_posts WHERE match_id=?", (match_id,))
                c.execute("DELETE FROM match_players WHERE match_id=?", (match_id,))
                c.execute("DELETE FROM shared_matches WHERE id=?", (match_id,))

    def change_player(self, match_id: str, user_id: int, *, join: bool) -> Match:
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            match = self._get(c, match_id)
            if match is None:
                raise MatchError("Dit groepsbericht is niet meer beschikbaar.")
            if match.status != "open" or match.started:
                raise MatchError("Deze reservatie is geannuleerd of al begonnen.")
            profile=c.execute('SELECT suspended FROM hub_profiles WHERE discord_id=?',(user_id,)).fetchone()
            if profile and profile['suspended']:
                raise MatchError('Dit profiel is gepauzeerd.')
            if match.hub_profile_id and (not match.hub_confirmed or not match.hub_visible):
                raise MatchError('Deze boeking is niet meer bevestigd of staat offline in de hub.')
            host=c.execute('SELECT suspended FROM hub_profiles WHERE id=?',(match.hub_profile_id,)).fetchone()
            if host and host['suspended']:
                raise MatchError('De organisator is gepauzeerd.')
            if join:
                if user_id in match.players:
                    raise MatchError("Je staat al op de deelnemerslijst.")
                if len(match.players) >= match.capacity:
                    raise MatchError("Het veld is vol: er zijn al vier spelers.")
                c.execute("INSERT INTO match_players VALUES (?,?,?)", (match_id, user_id, time.time()))
            else:
                if user_id == match.owner_id:
                    raise MatchError("Jij bent de organisator. De reservatie annuleren kan via `/bookings`.")
                if user_id not in match.players:
                    raise MatchError("Je staat niet op de deelnemerslijst.")
                c.execute("DELETE FROM match_players WHERE match_id=? AND user_id=?", (match_id, user_id))
            return self._get(c, match_id)

    @staticmethod
    def queue_hub_update(c,match_id):
        c.execute('''INSERT INTO hub_post_updates(match_id) VALUES(?) ON CONFLICT(match_id)
            DO UPDATE SET revision=revision+1,retry_at=0''',(match_id,))

    def cancel_booking(self, owner_id: int, booking_id: int) -> Match | None:
        with self._connection() as c:
            c.execute("UPDATE shared_matches SET status='cancelled' WHERE owner_id=? AND booking_id=?", (owner_id, booking_id))
            row = c.execute("SELECT id FROM shared_matches WHERE owner_id=? AND booking_id=?", (owner_id, booking_id)).fetchone()
            if row:self.queue_hub_update(c,row['id'])
            return self._get(c, row[0]) if row else None

    def future_matches(self) -> list[Match]:
        with self._connection() as c:
            rows = c.execute("SELECT id FROM shared_matches WHERE start_at>?", (time.time(),)).fetchall()
            return [self._get(c, row[0]) for row in rows]

    @staticmethod
    def _post(row):
        return MatchPost(row["id"], row["match_id"], row["channel_id"], row["guild_id"], row["message_id"],
            row["state"], row["replacement_id"], bool(row["move_requested"])) if row else None

    def get_post(self, post_id):
        with self._connection() as c:
            return self._post(c.execute("SELECT * FROM shared_match_posts WHERE id=?", (post_id,)).fetchone())

    def posts(self, match_id, *, active_only=False):
        with self._connection() as c:
            query = "SELECT * FROM shared_match_posts WHERE match_id=?" + (" AND state='active'" if active_only else "")
            return [self._post(row) for row in c.execute(query + " ORDER BY rowid", (match_id,))]

    def post_in_channel(self, match_id, channel_id):
        with self._connection() as c:
            return self._post(c.execute("SELECT * FROM shared_match_posts WHERE match_id=? AND channel_id=? AND state='active'", (match_id, channel_id)).fetchone())

    def reserve_post(self, match_id, owner_id, channel_id, guild_id, *, move=False):
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            match = self._get(c, match_id)
            if match is None or match.owner_id != owner_id or match.status != "open" or match.started:
                raise MatchError("Deze reservatie kan niet meer gedeeld worden.")
            row = c.execute("SELECT * FROM shared_match_posts WHERE match_id=? AND channel_id=? AND state='active'", (match_id, channel_id)).fetchone()
            if row:
                return self._post(row), False
            post_id = uuid.uuid4().hex
            c.execute("INSERT INTO shared_match_posts(id,match_id,channel_id,guild_id,move_requested) VALUES (?,?,?,?,?)",
                (post_id, match_id, channel_id, guild_id, int(move)))
            return self._post(c.execute("SELECT * FROM shared_match_posts WHERE id=?", (post_id,)).fetchone()), True

    def bind_post(self, post_id, channel_id, message_id):
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            post = self._post(c.execute("SELECT * FROM shared_match_posts WHERE id=?", (post_id,)).fetchone())
            if post is None or post.channel_id != channel_id or post.message_id not in (None, message_id):
                raise MatchError("Deze knop hoort niet bij dit groepsbericht.")
            c.execute("UPDATE shared_match_posts SET message_id=? WHERE id=?", (message_id, post_id))
            match = self._get(c, post.match_id)
            if post.state == "active" and (post.id == match.id or match.message_id is None):
                c.execute("UPDATE shared_matches SET channel_id=?,guild_id=?,message_id=? WHERE id=?",
                    (channel_id, post.guild_id, message_id, post.match_id))
            return self._post(c.execute("SELECT * FROM shared_match_posts WHERE id=?", (post_id,)).fetchone())

    def drop_pending_post(self, post_id):
        with self._connection() as c:
            c.execute("DELETE FROM shared_match_posts WHERE id=? AND message_id IS NULL", (post_id,))

    def move_posts(self, match_id, owner_id, target_id):
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            match = self._get(c, match_id)
            target = self._post(c.execute("SELECT * FROM shared_match_posts WHERE id=?", (target_id,)).fetchone())
            if match is None or match.owner_id != owner_id or match.status != "open" or match.started or target is None or target.match_id != match_id or target.state != "active" or target.message_id is None:
                raise MatchError("Het nieuwe groepsbericht is nog niet bevestigd; de oude berichten blijven behouden.")
            c.execute("UPDATE shared_match_posts SET state='retired',replacement_id=? WHERE match_id=? AND id!=? AND state='active'",
                (target_id, match_id, target_id))
            c.execute("UPDATE shared_match_posts SET replacement_id=? WHERE match_id=? AND state='retired'",
                (target_id, match_id))
            c.execute("UPDATE shared_match_posts SET move_requested=0 WHERE id=?", (target_id,))
            c.execute("UPDATE shared_matches SET channel_id=?,guild_id=?,message_id=? WHERE id=?",
                (target.channel_id, target.guild_id, target.message_id, match_id))
            return [self._post(row) for row in c.execute("SELECT * FROM shared_match_posts WHERE match_id=? AND state='retired'", (match_id,))]

    def retire_post(self, post_id, owner_id, *, removed=False):
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            post = self._post(c.execute("SELECT * FROM shared_match_posts WHERE id=?", (post_id,)).fetchone())
            match = self._get(c, post.match_id) if post else None
            if match is None or match.owner_id != owner_id:
                raise MatchError("Alleen de organisator kan dit groepsbericht verwijderen.")
            c.execute("UPDATE shared_match_posts SET state=? WHERE id=?", ("removed" if removed else "retired", post_id))
            if match.channel_id == post.channel_id and match.message_id == post.message_id:
                other = c.execute("SELECT * FROM shared_match_posts WHERE match_id=? AND state='active' AND message_id IS NOT NULL ORDER BY rowid LIMIT 1", (post.match_id,)).fetchone()
                c.execute("UPDATE shared_matches SET channel_id=?,guild_id=?,message_id=? WHERE id=?",
                    (other["channel_id"] if other else match.channel_id, other["guild_id"] if other else match.guild_id,
                     other["message_id"] if other else None, match.id))

    def queue_notice(self, post_id, channel_id, message_id, *, ttl=3600):
        with self._connection() as c:
            post = self._post(c.execute("SELECT * FROM shared_match_posts WHERE id=?", (post_id,)).fetchone())
            if post is None or post.state != "active" or post.channel_id != channel_id or post.guild_id is None:
                raise MatchError("Deze melding kan hier niet automatisch verwijderd worden.")
            if c.execute("SELECT 1 FROM shared_match_posts WHERE channel_id=? AND message_id=?", (channel_id, message_id)).fetchone():
                raise MatchError("Het oorspronkelijke groepsbericht blijft behouden.")
            c.execute("INSERT OR IGNORE INTO match_notices(channel_id,message_id,match_id,due_at) VALUES (?,?,?,?)",
                (channel_id, message_id, post.match_id, time.time() + ttl))

    def due_notices(self):
        with self._connection() as c:
            return [dict(row) for row in c.execute("SELECT * FROM match_notices WHERE due_at<=? AND retry_at<=? ORDER BY due_at LIMIT 50",
                (time.time(), time.time()))]

    def notice_deleted(self, channel_id, message_id):
        with self._connection() as c:
            c.execute("DELETE FROM match_notices WHERE channel_id=? AND message_id=?", (channel_id, message_id))

    def retry_notice(self, channel_id, message_id):
        with self._connection() as c:
            row = c.execute("SELECT attempts FROM match_notices WHERE channel_id=? AND message_id=?", (channel_id, message_id)).fetchone()
            if row:
                delay = min(3600, 60 * 2 ** min(row[0], 6))
                c.execute("UPDATE match_notices SET attempts=attempts+1,retry_at=? WHERE channel_id=? AND message_id=?",
                    (time.time() + delay, channel_id, message_id))
