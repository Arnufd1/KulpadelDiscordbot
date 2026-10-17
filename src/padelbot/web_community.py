"""The website uses Discord's canonical match roster, with private KUL verification snapshots."""
from __future__ import annotations
from datetime import datetime,timezone
from pathlib import Path
import os,time,uuid
from .web_store import PlatformStore
from .matches import MatchStore,MatchError
from .hub import HubStore,public_name


def timestamp(value):
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed).timestamp()


class CommunityBookings:
    def __init__(self,store:PlatformStore,hub_path=None):
        from .config import Settings
        self.store=store
        base=Settings()
        path=hub_path or os.environ.get('PADELWEB_HUB_DB')
        if path is None:
            path=base.padelbot_rules_file if store.root==Path('data/platform').resolve() else store.root/'hub.sqlite3'
        self.matches=MatchStore(Path(path))
        self.hub=HubStore(self.matches)
        with store.conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS community_matches (
                    id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),booking_id INTEGER NOT NULL,
                    title TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,start_ts REAL NOT NULL,
                    confirmed INTEGER NOT NULL,shared INTEGER NOT NULL DEFAULT 0,verified REAL NOT NULL,
                    UNIQUE(user_id,booking_id));
                CREATE TABLE IF NOT EXISTS community_players (
                    match_id TEXT NOT NULL REFERENCES community_matches(id),user_id TEXT NOT NULL REFERENCES users(id),
                    joined REAL NOT NULL,PRIMARY KEY(match_id,user_id));
            """)
            users=[dict(r) for r in c.execute('SELECT id,name,owner,suspended,avatar_url FROM users')]
        for user in users:self.hub.register(user,owner_discord_id=base.discord_owner_id)

    def sync(self,user_id,bookings,*,device_verified):
        now=time.time()
        user=self.store.user(user_id)
        if not user:return
        self.hub.register(user)
        valid={}
        with self.store.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE community_matches SET confirmed=0 WHERE user_id=?',(user_id,))
            for booking in bookings:
                if not booking.get('confirmed') or not device_verified:continue
                try:
                    start,end=timestamp(booking['start']),timestamp(booking['end'])
                    bid=int(booking['id'])
                except (KeyError,ValueError,TypeError):continue
                if bid<=0 or start<=now or end<=start:continue
                valid[bid]=booking
                c.execute("""INSERT INTO community_matches(id,user_id,booking_id,title,start,end,start_ts,confirmed,verified)
                    VALUES(?,?,?,?,?,?,?,1,?) ON CONFLICT(user_id,booking_id) DO UPDATE SET title=excluded.title,
                    start=excluded.start,end=excluded.end,start_ts=excluded.start_ts,confirmed=1,verified=excluded.verified""",
                    (uuid.uuid4().hex,user_id,bid,str(booking.get('name') or 'Padel')[:100],booking['start'],booking['end'],start,now))
        with self.matches._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            rows=c.execute("SELECT * FROM shared_matches WHERE hub_profile_id=? AND start_at>?",(user_id,now)).fetchall()
            for row in rows:
                if row['status']=='draft':
                    if row['booking_id'] not in valid:continue
                    c.execute("UPDATE hub_drafts SET state='confirmed',error=NULL WHERE id=?",(row['id'],))
                    c.execute("UPDATE shared_matches SET status='open',hub_visible=1 WHERE id=?",(row['id'],))
                booking=valid.get(row['booking_id'])
                changed=bool(row['hub_confirmed'])!=bool(booking)
                if booking and row['status'] in {'open','draft'}:
                    moved=timestamp(row['start_iso'])!=timestamp(booking['start']) or timestamp(row['end_iso'])!=timestamp(booking['end'])
                    if moved:
                        c.execute('DELETE FROM match_players WHERE match_id=? AND user_id!=?',(row['id'],row['owner_id']))
                        c.execute('UPDATE shared_matches SET hub_visible=0 WHERE id=?',(row['id'],))
                    c.execute("""UPDATE shared_matches SET start_iso=?,end_iso=?,start_at=?,court=?,hub_confirmed=1,hub_verified=? WHERE id=?""",
                        (booking['start'],booking['end'],timestamp(booking['start']),str(booking.get('name') or 'Padel')[:100],now,row['id']))
                    changed=changed or moved
                else:c.execute('UPDATE shared_matches SET hub_confirmed=0,hub_verified=? WHERE id=?',(now,row['id']))
                if changed:self.matches.queue_hub_update(c,row['id'])

    def share(self,user_id,booking_id,shared):
        if shared:self.store.assert_writable(user_id)
        with self.store.conn() as c:
            row=c.execute('SELECT * FROM community_matches WHERE user_id=? AND booking_id=?',(user_id,booking_id)).fetchone()
        with self.matches._connection() as c:
            actor=self.hub.actor(c,user_id,write=shared)
        match=self.matches.for_booking(actor,booking_id)
        if not shared:
            if not match:raise ValueError('Vernieuw eerst je eigen boekingen.')
        else:
            if not row or not row['confirmed'] or row['verified']<time.time()-300 or row['start_ts']<=time.time():
                raise ValueError('Vernieuw eerst je boekingen. Alleen betaalde, bevestigde reservaties kunnen online.')
            if match is None:
                try:match=self.matches.create(owner_id=actor,booking_id=booking_id,start_iso=row['start'],end_iso=row['end'],court=row['title'],channel_id=None,guild_id=None)
                except MatchError as error:raise ValueError(str(error)) from None
        with self.matches._connection() as c:
            c.execute("""UPDATE shared_matches SET hub_profile_id=?,hub_visible=?,hub_verified=?,hub_confirmed=? WHERE id=?""",
                (user_id,int(shared),row['verified'] if row else match.hub_verified,int(bool(row and row['confirmed'])),match.id))
            self.matches.queue_hub_update(c,match.id)
        with self.store.conn() as c:c.execute('UPDATE community_matches SET shared=? WHERE user_id=? AND booking_id=?',(int(shared),user_id,booking_id))
        self.store.audit(user_id,'match_shared' if shared else 'match_unshared',match.id)

    def own_shared(self,user_id):
        with self.matches._connection() as c:
            return {r['booking_id']:bool(r['hub_visible']) for r in c.execute('SELECT booking_id,hub_visible FROM shared_matches WHERE hub_profile_id=?',(user_id,))}

    def list(self,user_id):
        user=self.store.user(user_id)
        if not user or user['suspended']:raise ValueError('Dit profiel is niet beschikbaar.')
        self.hub.register(user)
        with self.matches._connection() as c:
            actor=self.hub.actor(c,user_id)
        result=[]
        for match in self.matches.future_matches():
            if match.status!='open' or not match.hub_visible or not match.hub_confirmed or match.hub_verified<time.time()-600:continue
            host=self.store.user(match.hub_profile_id) if match.hub_profile_id else None
            if not host or host['suspended']:continue
            joined=actor in match.players
            if len(match.players)>=4 and not joined:continue
            result.append({'id':match.id,'title':match.court,'start':match.start_iso,'end':match.end_iso,
                'host':match.names.get(match.owner_id,'Speler'),'players':[match.names[p] for p in match.players],
                'player_profiles':[self.hub.player_identity(p) for p in match.players],
                'count':len(match.players),'capacity':4,'joined':joined,'organiser':match.owner_id==actor,
                'verified':match.hub_verified,'origin':'discord' if self.matches.posts(match.id,active_only=True) else 'web','kind':'confirmed'})
        return sorted(result,key=lambda row:timestamp(row['start']))[:100]

    def host(self,match_id):
        match=self.matches.get(match_id)
        host=self.store.user(match.hub_profile_id) if match and match.hub_profile_id else None
        if not match or not host or host['suspended'] or not match.hub_visible or match.started or match.status!='open':
            raise ValueError('Deze online boeking is niet meer beschikbaar.')
        return {'id':match.id,'user_id':host['id'],'booking_id':match.booking_id,'kind':'confirmed'}

    def join_verified(self,match_id,user_id):
        self.store.assert_writable(user_id)
        user=self.store.user(user_id)
        if not user or user['suspended']:raise ValueError('Dit profiel is gepauzeerd.')
        self.hub.register(user)
        self.hub.join(match_id,user_id)
        self.store.audit(user_id,'match_joined',match_id)

    def leave(self,match_id,user_id):
        self.hub.leave(match_id,user_id)
        self.store.audit(user_id,'match_left',match_id)

    def hosts_to_refresh(self):
        with self.matches._connection() as c:
            return [r[0] for r in c.execute("SELECT DISTINCT hub_profile_id FROM shared_matches WHERE status='open' AND hub_visible=1 AND start_at>? AND hub_profile_id IS NOT NULL",(time.time(),))]
