"""Verified web/Discord identities share the existing match roster."""
import base64
import hashlib
import re
import secrets
import time

from .matches import MatchError,MatchStore
from .identity import google_avatar,discord_image


def public_name(value):
    words=str(value).split()
    return words[0][:60] if words and '@' not in words[0] else 'Speler'


class HubStore:
    def __init__(self,matches:MatchStore):
        self.matches=matches

    @staticmethod
    def actor(c,profile_id,*,write=False):
        row=c.execute('SELECT * FROM hub_profiles WHERE id=?',(profile_id,)).fetchone()
        if not row or row['suspended']:
            raise ValueError('Dit profiel is niet beschikbaar.')
        actor=row['discord_id'] if row['discord_id'] is not None else 'web:'+profile_id
        MatchStore.require_actor(c,actor,write=write)
        return actor

    def register(self,user,*,owner_discord_id=0):
        with self.matches._connection() as c:
            c.execute('''INSERT INTO hub_profiles(id,name,suspended,google_avatar) VALUES(?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,suspended=excluded.suspended,
                google_avatar=excluded.google_avatar''',
                (user['id'],public_name(user['name']),int(user.get('suspended',0)),google_avatar(user.get('avatar_url'))))
        if user.get('owner') and owner_discord_id:
            self.link(user['id'],owner_discord_id)

    def link(self,profile_id,discord_id):
        if not isinstance(discord_id,int) or discord_id<=0:
            raise ValueError('Ongeldige Discord-identiteit.')
        with self.matches._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM hub_profiles WHERE id=?',(profile_id,)).fetchone()
            if not row or row['suspended']:
                raise ValueError('Dit profiel is niet beschikbaar.')
            self.matches.require_actor(c,'web:'+profile_id,write=False)
            self.matches.require_actor(c,discord_id,write=False)
            other=c.execute('SELECT id FROM hub_profiles WHERE discord_id=?',(discord_id,)).fetchone()
            if other and other['id']!=profile_id:
                raise ValueError('Dit Discord-account is al aan een ander profiel gekoppeld.')
            if row['discord_id'] is not None:
                if row['discord_id']!=discord_id:
                    raise ValueError('Dit profiel is al aan een ander Discord-account gekoppeld.')
                c.execute('UPDATE shared_matches SET hub_profile_id=? WHERE owner_id=?',(profile_id,discord_id))
                return
            old='web:'+profile_id
            affected=[r[0] for r in c.execute('SELECT match_id FROM match_players WHERE user_id=?',(old,))]
            c.execute('UPDATE hub_profiles SET discord_id=? WHERE id=?',(discord_id,profile_id))
            c.execute('''INSERT OR IGNORE INTO match_players(match_id,user_id,joined_at)
                SELECT match_id,?,joined_at FROM match_players WHERE user_id=?''',(discord_id,old))
            c.execute('DELETE FROM match_players WHERE user_id=?',(old,))
            c.execute('UPDATE shared_matches SET owner_id=? WHERE owner_id=?',(discord_id,old))
            c.execute('UPDATE shared_matches SET hub_profile_id=? WHERE owner_id=?',(profile_id,discord_id))
            for match_id in affected:self.matches.queue_hub_update(c,match_id)

    def linked(self,profile_id):
        with self.matches._connection() as c:
            row=c.execute('SELECT discord_id FROM hub_profiles WHERE id=?',(profile_id,)).fetchone()
        return bool(row and row['discord_id'] is not None)

    def new_code(self,profile_id):
        if self.linked(profile_id):raise ValueError('Je Discord-account is al gekoppeld.')
        code=base64.b32encode(secrets.token_bytes(10)).decode()
        with self.matches._connection() as c:
            self.actor(c,profile_id)
            c.execute('DELETE FROM hub_link_codes WHERE profile_id=? OR expires<?',(profile_id,time.time()))
            c.execute('INSERT INTO hub_link_codes VALUES(?,?,?)',(hashlib.sha256(code.encode()).hexdigest(),profile_id,time.time()+600))
        return code

    def consume_code(self,code,discord_id):
        code=code.strip().upper()
        if not re.fullmatch('[A-Z2-7]{16}',code):raise ValueError('Ongeldige of verlopen koppelcode.')
        digest=hashlib.sha256(code.encode()).hexdigest()
        # Claim once before linking. A failed attempt cannot be replayed by another account.
        with self.matches._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM hub_link_codes WHERE code=? AND expires>?',(digest,time.time())).fetchone()
            if not row:raise ValueError('Ongeldige of verlopen koppelcode.')
            c.execute('DELETE FROM hub_link_codes WHERE code=?',(digest,))
        self.link(row['profile_id'],discord_id)

    def remember_discord(self,user_id,name,avatar=None):
        if not isinstance(user_id,int) or isinstance(user_id,bool) or user_id<=0:
            raise ValueError('Ongeldige Discord-identiteit.')
        name=' '.join(str(name).split())[:80] or 'Discord-speler'
        avatar=discord_image(avatar)
        with self.matches._connection() as c:
            c.execute('''INSERT INTO hub_discord_names(id,name,avatar,seen_at) VALUES(?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                avatar=COALESCE(excluded.avatar,hub_discord_names.avatar),seen_at=excluded.seen_at''',
                      (user_id,name,avatar,time.time()))

    def remember_guild(self,guild_id,name,*,active=True):
        if not isinstance(guild_id,int) or isinstance(guild_id,bool) or guild_id<=0:return
        with self.matches._connection() as c:
            c.execute('''INSERT INTO hub_discord_guilds(id,name,active,last_seen) VALUES(?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,active=excluded.active,last_seen=excluded.last_seen''',
                (guild_id,' '.join(str(name).split())[:100],int(active),time.time()))

    def sync_guilds(self,guilds):
        with self.matches._connection() as c:
            c.execute('UPDATE hub_discord_guilds SET active=0')
        for guild in guilds:self.remember_guild(guild.id,guild.name)

    def guilds(self):
        with self.matches._connection() as c:
            return [dict(row) for row in c.execute('SELECT id,name,active,last_seen FROM hub_discord_guilds ORDER BY name,id')]

    def observe_context(self,discord_id,context,*,guild_id=0):
        if context not in {'guild','personal','private','oauth'}:return
        if not isinstance(discord_id,int) or discord_id<=0:return
        with self.matches._connection() as c:
            c.execute('''INSERT INTO hub_discord_contexts(discord_id,context,guild_id,last_seen) VALUES(?,?,?,?)
                ON CONFLICT(discord_id,context,guild_id) DO UPDATE SET last_seen=excluded.last_seen''',
                (discord_id,context,int(guild_id or 0),time.time()))

    def backfill_shared_contexts(self):
        """Published posts prove use there, not membership or a personal install."""
        with self.matches._connection() as c:
            rows=c.execute('''SELECT m.owner_id,p.guild_id,MAX(player.joined_at) observed
                FROM shared_matches m JOIN shared_match_posts p ON p.match_id=m.id
                JOIN match_players player ON player.match_id=m.id AND player.user_id=m.owner_id
                WHERE typeof(m.owner_id)='integer' AND m.owner_id>0 AND p.message_id IS NOT NULL
                GROUP BY m.owner_id,p.guild_id''').fetchall()
            for row in rows:
                c.execute('''INSERT OR IGNORE INTO hub_discord_contexts(discord_id,context,guild_id,last_seen)
                    VALUES(?,?,?,?)''',(row['owner_id'],'guild' if row['guild_id'] else 'private',
                                        row['guild_id'] or 0,row['observed']))

    def discord_profile(self,discord_id):
        with self.matches._connection() as c:
            row=c.execute('SELECT * FROM hub_discord_names WHERE id=?',(discord_id,)).fetchone()
            contexts=[dict(r) for r in c.execute('''SELECT x.context,NULLIF(x.guild_id,0) guild_id,
                g.name guild_name,x.last_seen FROM hub_discord_contexts x LEFT JOIN hub_discord_guilds g ON g.id=x.guild_id
                WHERE x.discord_id=? ORDER BY x.last_seen DESC''',(discord_id,))]
            guilds=[dict(r) for r in c.execute('''SELECT DISTINCT g.* FROM hub_discord_guilds g
                JOIN hub_discord_contexts x ON g.id=x.guild_id WHERE x.discord_id=? ORDER BY g.name''',(discord_id,))]
        return {'discord_id':int(discord_id),'name':row['name'] if row else 'Discord-speler',
                'avatar':discord_image(row['avatar']) if row else None,'avatar_url':discord_image(row['avatar']) if row else None,
                'seen_at':row['seen_at'] if row else 0,'contexts':contexts,'guilds':guilds}

    def profile_identity(self,profile_id):
        with self.matches._connection() as c:
            row=c.execute('SELECT * FROM hub_profiles WHERE id=?',(profile_id,)).fetchone()
        if not row:return None
        profile=self.discord_profile(row['discord_id']) if row['discord_id'] is not None else None
        avatar=(profile['avatar'] if profile else None) or google_avatar(row['google_avatar'])
        return {'id':row['id'],'name':profile['name'] if profile and profile['seen_at'] else row['name'],
                'avatar':avatar,'avatar_url':avatar,'registered':True,'discord_id':row['discord_id']}

    def player_identity(self,actor):
        with self.matches._connection() as c:
            row=c.execute('SELECT id FROM hub_profiles WHERE discord_id=?',(actor,)).fetchone() if isinstance(actor,int) else None
        profile_id=row['id'] if row else str(actor).removeprefix('web:') if not isinstance(actor,int) else None
        if profile_id:return self.profile_identity(profile_id)
        profile=self.discord_profile(actor)
        return {'id':'discord:'+str(actor),'name':profile['name'],'avatar':profile['avatar'],
                'avatar_url':profile['avatar'],'registered':False,'discord_id':int(actor)}

    def join(self,match_id,profile_id,*,fresh=True):
        with self.matches._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            actor=self.actor(c,profile_id,write=True)
            match=self.matches._get(c,match_id)
            if (not match or match.started or match.status!='open' or not match.hub_confirmed or not match.hub_visible
                    or (fresh and match.hub_verified<time.time()-60)):
                raise ValueError('Deze boeking is niet meer bevestigd of beschikbaar.')
            host=c.execute('SELECT suspended FROM hub_profiles WHERE id=?',(match.hub_profile_id,)).fetchone()
            if not host or host['suspended']:raise ValueError('Deze boeking is niet beschikbaar.')
            if actor in match.players:return
            if len(match.players)>=4:raise ValueError('Deze match is volzet.')
            c.execute('INSERT INTO match_players VALUES(?,?,?)',(match_id,actor,time.time()))
            self.matches.queue_hub_update(c,match_id)

    def leave(self,match_id,profile_id):
        with self.matches._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            actor=self.actor(c,profile_id)
            match=self.matches._get(c,match_id)
            if match and match.owner_id==actor:raise ValueError('Zet als organisator de boeking offline via Mijn boekingen.')
            c.execute('DELETE FROM match_players WHERE match_id=? AND user_id=?',(match_id,actor))
            if match:self.matches.queue_hub_update(c,match_id)
