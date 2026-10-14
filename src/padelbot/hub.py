"""Verified web/Discord identities share the existing match roster."""
import base64
import hashlib
import re
import secrets
import time

from .matches import MatchError,MatchStore


def public_name(value):
    words=str(value).split()
    return words[0][:60] if words and '@' not in words[0] else 'Speler'


class HubStore:
    def __init__(self,matches:MatchStore):
        self.matches=matches

    @staticmethod
    def actor(c,profile_id):
        row=c.execute('SELECT * FROM hub_profiles WHERE id=?',(profile_id,)).fetchone()
        if not row or row['suspended']:
            raise ValueError('Dit profiel is niet beschikbaar.')
        return row['discord_id'] if row['discord_id'] is not None else 'web:'+profile_id

    def register(self,user,*,owner_discord_id=0):
        with self.matches._connection() as c:
            c.execute('''INSERT INTO hub_profiles(id,name,suspended) VALUES(?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,suspended=excluded.suspended''',
                (user['id'],public_name(user['name']),int(user.get('suspended',0))))
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

    def remember_discord(self,user_id,name):
        with self.matches._connection() as c:
            c.execute('INSERT INTO hub_discord_names VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name',
                      (user_id,public_name(name)))

    def join(self,match_id,profile_id,*,fresh=True):
        with self.matches._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            actor=self.actor(c,profile_id)
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
