"""Opt-in matches backed by the organiser's verified KUL reservations."""
from __future__ import annotations

from datetime import datetime, timezone
import time
import uuid

from .web_store import PlatformStore


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def public_name(value):
    words=str(value).split()
    return words[0] if words and '@' not in words[0] else 'Speler'


class CommunityBookings:
    def __init__(self, store: PlatformStore):
        self.store = store
        with store.conn() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS community_matches (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    booking_id INTEGER NOT NULL, title TEXT NOT NULL, start TEXT NOT NULL,
                    end TEXT NOT NULL, start_ts REAL NOT NULL, confirmed INTEGER NOT NULL,
                    shared INTEGER NOT NULL DEFAULT 0, verified REAL NOT NULL,
                    UNIQUE(user_id,booking_id));
                CREATE TABLE IF NOT EXISTS community_players (
                    match_id TEXT NOT NULL REFERENCES community_matches(id),
                    user_id TEXT NOT NULL REFERENCES users(id), joined REAL NOT NULL,
                    PRIMARY KEY(match_id,user_id));
                CREATE UNIQUE INDEX IF NOT EXISTS single_shared_booking
                    ON community_matches(booking_id) WHERE shared=1;
            ''')

    def sync(self, user_id: str, bookings: list[dict], *, device_verified: bool):
        now = time.time()
        with self.store.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE community_matches SET confirmed=0 WHERE user_id=?',(user_id,))
            for booking in bookings:
                if not booking.get('confirmed') or not device_verified:
                    continue
                try:
                    start = timestamp(booking['start'])
                    end = timestamp(booking['end'])
                    booking_id = int(booking['id'])
                except (KeyError,ValueError,TypeError):
                    continue
                if booking_id<=0 or start<=now or end<=start:
                    continue
                existing = c.execute('SELECT * FROM community_matches WHERE user_id=? AND booking_id=?',(user_id,booking_id)).fetchone()
                match_id = existing['id'] if existing else uuid.uuid4().hex
                if existing and (existing['start']!=booking['start'] or existing['end']!=booking['end']):
                    # A rescheduled reservation requires renewed participation and sharing.
                    c.execute('DELETE FROM community_players WHERE match_id=?',(match_id,))
                    c.execute('UPDATE community_matches SET shared=0 WHERE id=?',(match_id,))
                c.execute('''INSERT INTO community_matches(id,user_id,booking_id,title,start,end,start_ts,confirmed,verified)
                    VALUES(?,?,?,?,?,?,?,1,?) ON CONFLICT(user_id,booking_id) DO UPDATE SET
                    title=excluded.title,start=excluded.start,end=excluded.end,start_ts=excluded.start_ts,
                    confirmed=1,verified=excluded.verified''',
                    (match_id,user_id,booking_id,str(booking.get('name') or 'Padel')[:100],booking['start'],booking['end'],start,now))
                c.execute('INSERT OR IGNORE INTO community_players VALUES(?,?,?)',(match_id,user_id,now))

    def share(self, user_id: str, booking_id: int, shared: bool):
        with self.store.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM community_matches WHERE user_id=? AND booking_id=?',(user_id,booking_id)).fetchone()
            if not row:
                raise ValueError('Vernieuw eerst je eigen boekingen bij KUL.')
            if shared and (not row['confirmed'] or row['verified']<time.time()-300 or row['start_ts']<=time.time()):
                raise ValueError('Vernieuw eerst je boekingen. Alleen bevestigde, betaalde reservaties kunnen online.')
            if shared and c.execute('SELECT 1 FROM community_matches WHERE booking_id=? AND shared=1 AND user_id!=?',(booking_id,user_id)).fetchone():
                raise ValueError('Deze KUL-reservering staat al online.')
            c.execute('UPDATE community_matches SET shared=? WHERE id=?',(int(shared),row['id']))
        self.store.audit(user_id,'match_shared' if shared else 'match_unshared',row['id'])

    def own_shared(self, user_id):
        with self.store.conn() as c:
            return {r['booking_id']:bool(r['shared']) for r in c.execute('SELECT booking_id,shared FROM community_matches WHERE user_id=?',(user_id,))}

    def list(self, user_id):
        with self.store.conn() as c:
            rows = c.execute('''SELECT m.*,u.name AS host FROM community_matches m JOIN users u ON u.id=m.user_id
                WHERE m.shared=1 AND m.confirmed=1 AND m.start_ts>? AND m.verified>? AND u.suspended=0
                ORDER BY m.start_ts LIMIT 100''',(time.time(),time.time()-600)).fetchall()
            result = []
            for row in rows:
                players = c.execute('''SELECT p.user_id,u.name FROM community_players p JOIN users u ON u.id=p.user_id
                    WHERE p.match_id=? AND u.suspended=0 ORDER BY p.joined,p.user_id''',(row['id'],)).fetchall()
                joined = any(p['user_id']==user_id for p in players)
                if len(players)>=4 and not joined:
                    continue
                result.append({'id':row['id'],'title':row['title'],'start':row['start'],'end':row['end'],
                    'host':public_name(row['host']),'players':[public_name(p['name']) for p in players],
                    'count':len(players),'capacity':4,'joined':joined,'organiser':row['user_id']==user_id,'verified':row['verified']})
        return result

    def host(self, match_id):
        with self.store.conn() as c:
            row = c.execute('''SELECT m.* FROM community_matches m JOIN users u ON u.id=m.user_id
                WHERE m.id=? AND m.shared=1 AND m.start_ts>? AND u.suspended=0''',(match_id,time.time())).fetchone()
        if not row:
            raise ValueError('Deze online boeking is niet meer beschikbaar.')
        return dict(row)

    def join_verified(self, match_id, user_id):
        with self.store.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('''SELECT m.* FROM community_matches m JOIN users u ON u.id=m.user_id
                WHERE m.id=? AND m.shared=1 AND m.confirmed=1 AND m.start_ts>? AND m.verified>?
                AND u.suspended=0''',(match_id,time.time(),time.time()-60)).fetchone()
            active = c.execute('SELECT suspended FROM users WHERE id=?',(user_id,)).fetchone()
            if not row or not active or active['suspended']:
                raise ValueError('Deze boeking is niet meer bevestigd of beschikbaar.')
            if c.execute('SELECT 1 FROM community_players WHERE match_id=? AND user_id=?',(match_id,user_id)).fetchone():
                return
            count = c.execute('''SELECT count(*) FROM community_players p JOIN users u ON u.id=p.user_id
                WHERE p.match_id=? AND u.suspended=0''',(match_id,)).fetchone()[0]
            if count>=4:
                raise ValueError('Deze match is volzet.')
            c.execute('INSERT INTO community_players VALUES(?,?,?)',(match_id,user_id,time.time()))
        self.store.audit(user_id,'match_joined',match_id)

    def leave(self, match_id, user_id):
        with self.store.conn() as c:
            row = c.execute('SELECT user_id FROM community_matches WHERE id=?',(match_id,)).fetchone()
            if row and row['user_id']==user_id:
                raise ValueError('Als organisator kun je de boeking offline zetten via Mijn boekingen.')
            c.execute('DELETE FROM community_players WHERE match_id=? AND user_id=?',(match_id,user_id))
        self.store.audit(user_id,'match_left',match_id)

    def hosts_to_refresh(self):
        with self.store.conn() as c:
            return [r['user_id'] for r in c.execute('''SELECT DISTINCT m.user_id FROM community_matches m
                JOIN users u ON u.id=m.user_id WHERE m.shared=1 AND m.start_ts>? AND u.suspended=0''',(time.time(),))]
