"""Owner-only projections: cached activity and identities, never credential material."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import time

from .web_runtime import tenant_env,rules_for


class AdminService:
    def __init__(self,platform):
        self.platform=platform
        self.store=platform.store
        self.community=platform.community
        self.hub=platform.hub
        self.matches=self.hub.matches

    def _discord(self,discord_id,*,linked):
        empty={'linked':False,'id':None,'name':None,'avatar_url':None,'profile_url':None,
               'guilds':[],'guilds_checked':None,'personal_install':'unknown','contexts':[]}
        if discord_id is None:return empty
        identity=self.hub.discord_profile(int(discord_id)) or {}
        contexts=[{**row,'guild_id':str(row['guild_id']) if row.get('guild_id') is not None else None}
                  for row in identity.get('contexts',[])]
        guilds=[{'id':str(row['id']),'name':row['name'],'installed':bool(row.get('active')),
                 'observed':row.get('last_seen'),'icon_url':row.get('icon_url')} for row in identity.get('guilds',[])]
        return {**empty,'linked':linked,'id':str(discord_id),'name':identity.get('name'),
                'avatar_url':identity.get('avatar_url') or identity.get('avatar'),
                'profile_url':'https://discord.com/users/'+str(discord_id),'guilds':guilds,
                'guilds_checked':max([row['observed'] for row in guilds if row.get('observed')]+[0]) or None,
                'personal_install':'known' if any(row.get('context')=='personal' for row in contexts) else 'unknown',
                'contexts':contexts}

    def _joined(self,actor):
        with self.matches._connection() as c:
            ids=[row[0] for row in c.execute('''SELECT m.id FROM shared_matches m JOIN match_players p ON p.match_id=m.id
                WHERE p.user_id=? AND m.owner_id!=? AND m.start_at>? ORDER BY m.start_at''',(actor,actor,time.time()))]
            matches=[self.matches._get(c,ident) for ident in ids]
        return [{'id':match.id,'title':match.court,'start':match.start_iso,'end':match.end_iso,
                 'host':match.names.get(match.owner_id,'Speler'),'count':len(match.players),
                 'capacity':match.capacity,'status':match.status} for match in matches if match]

    def user(self,user_id):
        user=self.store.user(user_id)
        if not user:raise ValueError('Profiel niet gevonden.')
        with self.matches._connection() as c:
            row=c.execute('SELECT discord_id FROM hub_profiles WHERE id=?',(user_id,)).fetchone()
        discord_id=row[0] if row else None
        discord=self._discord(discord_id,linked=discord_id is not None)
        access=self.platform.access_status(user_id)
        env=tenant_env(self.store,user)
        profile=Path(env['PADELBOT_ANDROID_PROFILE_FILE'])
        try:registered=json.loads(profile.read_text()).get('enrollment_verified') is True
        except (OSError,ValueError,AttributeError):registered=False
        account=self.platform.account_status(user_id)
        check=account.get('check') or {}
        payment=self.platform.payment_status(user_id)
        with self.store.conn() as c:
            rows=c.execute('SELECT booking_id,title,start,end,confirmed,shared,verified FROM community_matches WHERE user_id=? AND start_ts>? ORDER BY start_ts',(user_id,time.time())).fetchall()
            last=c.execute("SELECT finished FROM jobs WHERE user_id=? AND kind='bookings' AND status='done' ORDER BY finished DESC LIMIT 1",(user_id,)).fetchone()
            pending=[dict(row) for row in c.execute("SELECT sale_id,booking_id,status,created FROM private_payments WHERE user_id=? AND status='pending' ORDER BY created DESC",(user_id,))]
        bookings=[{'id':row['booking_id'],**{key:row[key] for key in ['title','start','end','confirmed','shared','verified']}} for row in rows]
        # An unverified snapshot is not an open, confirmed KUL booking.
        bookings=[row for row in bookings if row['confirmed']]
        checked=last[0] if last else max([row['verified'] for row in bookings]+[0]) or None
        needs_attention=Path(env['PADELBOT_ANDROID_FAILURE_FILE']).exists()
        session_saved=Path(env['PADELBOT_TOKEN_FILE']).exists()
        return {'profile':{'id':user['id'],'name':user['name'],'email':user['email'],'owner':bool(user['owner']),
                    **access,'created':user['created'],
                    'google_linked':bool(user['google_sub']),'avatar_url':discord['avatar_url'] or user.get('avatar_url')},
                'discord':discord,'kul':{'registered':registered,'session_saved':session_saved,
                    'verified':bool(registered and session_saved and not needs_attention and check.get('status')=='done'),
                    'checked':check.get('checked'),'needs_attention':needs_attention},
                'bookings':bookings,'bookings_checked':checked,'pending_payments':pending,
                'rules':[asdict(rule) for rule in rules_for(self.store,user).list_rules() if rule.enabled],
                'payment':{'linked':bool(payment and payment.get('stored_card')),'checked':payment.get('checked') if payment else None},
                'joins':self._joined(discord_id if discord_id is not None else 'web:'+user_id)}

    def guests(self):
        with self.matches._connection() as c:
            ids=[row[0] for row in c.execute('''SELECT id FROM (
                SELECT id FROM hub_discord_names UNION SELECT user_id id FROM match_players WHERE typeof(user_id)='integer'
                UNION SELECT owner_id id FROM shared_matches WHERE typeof(owner_id)='integer') WHERE id>0 AND id NOT IN
                (SELECT discord_id FROM hub_profiles WHERE discord_id IS NOT NULL) ORDER BY id''')]
        return [self.guest(str(ident))['profile'] for ident in ids]

    def guest(self,discord_id):
        if not str(discord_id).isdigit() or int(discord_id)<=0:raise ValueError('Discord-profiel niet gevonden.')
        discord_id=int(discord_id)
        with self.matches._connection() as c:
            if c.execute('SELECT 1 FROM hub_profiles WHERE discord_id=?',(discord_id,)).fetchone():
                raise ValueError('Dit Discord-account heeft een geregistreerd profiel.')
            if not (c.execute('SELECT 1 FROM hub_discord_names WHERE id=?',(discord_id,)).fetchone()
                    or c.execute('SELECT 1 FROM match_players WHERE user_id=?',(discord_id,)).fetchone()
                    or c.execute('SELECT 1 FROM shared_matches WHERE owner_id=?',(discord_id,)).fetchone()):
                raise ValueError('Discord-profiel niet gevonden.')
            moderation=c.execute('SELECT suspended,restricted FROM hub_moderation WHERE actor=?',('discord:'+str(discord_id),)).fetchone()
        discord=self._discord(discord_id,linked=False)
        return {'profile':{'id':str(discord_id),'name':discord['name'] or 'Discord-speler','email':None,'owner':False,
                    'suspended':bool(moderation and moderation['suspended']),'restricted':bool(moderation and moderation['restricted']),
                    'created':None,'google_linked':False,'avatar_url':discord['avatar_url']},
                'discord':discord,'kul':{'registered':False,'session_saved':False,'verified':False,'checked':None,'needs_attention':False},
                'bookings':[],'bookings_checked':None,'pending_payments':[],'rules':[],
                'payment':{'linked':False,'checked':None},'joins':self._joined(discord_id)}

    def users(self):
        with self.store.conn() as c:
            rows=[dict(row) for row in c.execute('SELECT id,email,name,owner,suspended,restricted,created,avatar_url FROM users ORDER BY created')]
        for row in rows:
            row.update(self.platform.access_status(row['id']))
            with self.matches._connection() as c:
                link=c.execute('SELECT discord_id FROM hub_profiles WHERE id=?',(row['id'],)).fetchone()
            row['discord_id']=str(link[0]) if link and link[0] is not None else None
            if row['discord_id']:
                row['avatar_url']=self._discord(row['discord_id'],linked=True)['avatar_url'] or row['avatar_url']
            row['connected']=Path(tenant_env(self.store,row)['PADELBOT_TOKEN_FILE']).exists()
            row['rules']=sum(1 for rule in rules_for(self.store,row).list_rules() if rule.enabled)
        return rows

    def moderate_user(self,actor,user_id,values):
        user=self.store.moderate(actor,user_id,**{**self.platform.access_status(user_id),**values})
        self.hub.register(user)
        with self.matches._connection() as c:
            row=c.execute('SELECT discord_id FROM hub_profiles WHERE id=?',(user_id,)).fetchone()
            keys=['profile:'+user_id]+(['discord:'+str(row[0])] if row and row[0] is not None else [])
            for key in keys:
                c.execute('''INSERT INTO hub_moderation(actor,suspended,restricted,updated) VALUES(?,?,?,?)
                    ON CONFLICT(actor) DO UPDATE SET suspended=excluded.suspended,restricted=excluded.restricted,updated=excluded.updated''',
                    (key,int(user['suspended']),int(user['restricted']),time.time()))
            if user['suspended']:
                c.execute('UPDATE shared_matches SET hub_visible=0 WHERE hub_profile_id=?',(user_id,))
        return self.user(user_id)

    def moderate_guest(self,actor,discord_id,values):
        guest=self.guest(discord_id)
        discord_id=guest['profile']['id']
        suspended=bool(values.get('suspended',guest['profile']['suspended']))
        restricted=bool(values.get('restricted',guest['profile']['restricted']))
        with self.matches._connection() as c:
            c.execute('''INSERT INTO hub_moderation(actor,suspended,restricted,updated) VALUES(?,?,?,?)
                ON CONFLICT(actor) DO UPDATE SET suspended=excluded.suspended,restricted=excluded.restricted,updated=excluded.updated''',
                ('discord:'+str(discord_id),int(suspended),int(restricted),time.time()))
        self.store.audit(actor,'discord_guest_moderated',str(discord_id))
        return self.guest(discord_id)
