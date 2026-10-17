"""Shared profile, booking queue and private Discord delivery for both frontends."""
from __future__ import annotations

import json
from pathlib import Path
import secrets
import time
from decimal import Decimal,InvalidOperation
from urllib.parse import urlencode

from cryptography.fernet import Fernet
from filelock import FileLock

from .web_store import PlatformStore,digest
from .web_community import CommunityBookings


def install_url(client_id: str, *, personal=False) -> str:
    if not str(client_id).isdigit():raise ValueError('Discord wordt nog ingesteld.')
    return 'https://discord.com/oauth2/authorize?'+urlencode({
        'client_id':client_id,'integration_type':1 if personal else 0,
        'scope':'applications.commands' if personal else 'bot applications.commands',
        'permissions':0 if personal else 84992})  # View/send/embed/read history; no administration.


def payment_mode(preferences):
    mode=preferences.get('payment_mode','kul')
    if mode not in {'kul','discord'}:raise ValueError('Kies een betaalwijze.')
    return mode


def validate_payment(me,amount,mode):
    from .payment import has_direct_debit_mandate
    if mode=='discord':return
    if mode!='kul':raise ValueError('Kies een betaalwijze.')
    try:
        balance=Decimal(str(me.get('balance') or 0))
        if balance.is_finite() and balance>=amount:return
        if has_direct_debit_mandate(me) and int(me.get('directDebitSpec') or 0)>0:return
    except (ValueError,TypeError,InvalidOperation):pass
    if me.get('_stored_method'):return
    raise ValueError('Koppel een KUL-betaalmethode of kies Betaallink via Discord.')


class PlatformService:
    def __init__(self,store:PlatformStore,community=None):
        self.store=store
        self.community=community or CommunityBookings(store)
        self.hub=self.community.hub
        with store.conn() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS discord_oauth (
                    state TEXT PRIMARY KEY,session TEXT NOT NULL,user_id TEXT NOT NULL,expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS discord_deliveries (
                    id TEXT PRIMARY KEY,user_id TEXT NOT NULL,discord_id INTEGER NOT NULL,
                    payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',created REAL NOT NULL,
                    finished REAL,error TEXT);
                CREATE TABLE IF NOT EXISTS platform_runtime (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS payment_accounts (
                    user_id TEXT PRIMARY KEY,balance TEXT NOT NULL,direct_debit INTEGER NOT NULL,checked REAL NOT NULL);
            ''')
            columns={r[1] for r in c.execute('PRAGMA table_info(discord_deliveries)')}
            for field in ['channel_id','message_id']:
                if field not in columns:c.execute(f'ALTER TABLE discord_deliveries ADD COLUMN {field} INTEGER')
            columns={r[1] for r in c.execute('PRAGMA table_info(payment_accounts)')}
            for field in ['stored_method','stored_card']:
                if field not in columns:c.execute(f'ALTER TABLE payment_accounts ADD COLUMN {field} INTEGER NOT NULL DEFAULT 0')

    def discord_id(self,user_id):
        with self.hub.matches._connection() as c:
            row=c.execute('SELECT discord_id FROM hub_profiles WHERE id=? AND suspended=0',(user_id,)).fetchone()
        return row[0] if row else None

    def discord_user(self,discord_id):
        with self.hub.matches._connection() as c:
            row=c.execute('SELECT id FROM hub_profiles WHERE discord_id=? AND suspended=0',(discord_id,)).fetchone()
        user=self.store.user(row[0]) if row else None
        if not user or user['suspended']:raise ValueError('Koppel eerst je profiel op padel.sigmadvisor.com.')
        return user

    def discord_online(self):
        with self.store.conn() as c:
            row=c.execute("SELECT value FROM platform_runtime WHERE key='discord_heartbeat'").fetchone()
        return bool(row and float(row[0])>time.time()-45)

    def enqueue(self,user,kind,payload):
        if not self.store.user(user['id']) or self.store.user(user['id'])['suspended']:
            raise ValueError('Dit profiel is gepauzeerd.')
        if kind not in {'slots','quote','book','bookings','login','account','rule','community_join'}:
            raise ValueError('Onbekende taak.')
        dedup=None
        if kind=='slots':
            if not 0<=int(payload['start'])<=int(payload['end'])<=30:raise ValueError('Kies een periode van 0 tot 30 dagen.')
        if kind in {'book','quote'}:
            from datetime import datetime
            if int(payload['product'])<=0 or int(payload['court'])<=0:raise ValueError('Ongeldig terrein.')
            if datetime.fromisoformat(payload['target'].replace('Z','+00:00')).tzinfo is None:raise ValueError('Ongeldig tijdstip.')
            amount=Decimal(str(payload.get('amount',1)))
            if not amount.is_finite() or not 0<amount<=100:raise ValueError('Ongeldig bedrag.')
            if kind=='book':
                # Both frontends use the same occurrence key, including Z/+00:00 aliases.
                target=datetime.fromisoformat(payload['target'].replace('Z','+00:00')).timestamp()
                dedup=f"manual:{user['id']}:{int(payload['product'])}:{int(payload['court'])}:{target}"
                # Preserve outstanding requests created before canonical timestamp keys.
                with self.store.conn() as c:
                    prior=c.execute("SELECT id,payload FROM jobs WHERE user_id=? AND kind='book' AND dedup IS NOT NULL ORDER BY created DESC LIMIT 100",(user['id'],)).fetchall()
                for row in prior:
                    previous=json.loads(row['payload'])
                    if (int(previous.get('product',0))==int(payload['product']) and int(previous.get('court',0))==int(payload['court'])
                        and datetime.fromisoformat(previous['target'].replace('Z','+00:00')).timestamp()==target):return row['id']
                if payment_mode(self.store.preferences(user['id']))=='discord' and not self.discord_id(user['id']):
                    raise ValueError('Koppel Discord om je betaallink te ontvangen.')
                if payment_mode(self.store.preferences(user['id']))=='discord' and not self.discord_online():
                    raise ValueError('De Discord-bot is tijdelijk offline. Er is niets geboekt.')
        return self.store.enqueue(user['id'],kind,payload,dedup=dedup)

    def oauth_start(self,user_id,session):
        state=secrets.token_urlsafe(32)
        with self.store.conn() as c:
            c.execute('DELETE FROM discord_oauth WHERE expires<?',(time.time(),))
            c.execute('INSERT INTO discord_oauth VALUES(?,?,?,?)',(digest(state),digest(session),user_id,time.time()+600))
        return state

    def oauth_consume(self,state,session,user_id):
        with self.store.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT state FROM discord_oauth WHERE state=? AND session=? AND user_id=? AND expires>?',
                (digest(state),digest(session),user_id,time.time())).fetchone()
            if row:c.execute('DELETE FROM discord_oauth WHERE state=?',(digest(state),))
        return bool(row)

    def remember_payment(self,user_id,summary):
        with self.store.conn() as c:
            c.execute('INSERT INTO payment_accounts(user_id,balance,direct_debit,checked,stored_method,stored_card) VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET balance=excluded.balance,direct_debit=excluded.direct_debit,checked=excluded.checked,stored_method=excluded.stored_method,stored_card=excluded.stored_card',
                (user_id,str(summary['balance']),int(summary['direct_debit']),time.time(),int(summary.get('stored_method',False)),int(summary.get('stored_card',False))))

    def payment_status(self,user_id):
        with self.store.conn() as c:
            row=c.execute('SELECT balance,direct_debit,checked,stored_method,stored_card FROM payment_accounts WHERE user_id=?',(user_id,)).fetchone()
        return dict(row) if row else None

    def cipher(self):
        path=self.store.root/'discord-delivery.key'
        with FileLock(str(path)+'.lock',timeout=15):
            if not path.exists():
                with path.open('xb') as f:f.write(Fernet.generate_key())
                path.chmod(0o600)
        return Fernet(path.read_bytes())

    def _queue_notice(self,user_id,ident,content):
        discord_id=self.discord_id(user_id)
        if not discord_id:raise ValueError('Geen gekoppeld Discord-account voor de betaallink.')
        content.update(user_id=user_id,discord_id=discord_id)
        payload=self.cipher().encrypt(json.dumps(content).encode()).decode()
        with self.store.conn() as c:
            c.execute('INSERT OR IGNORE INTO discord_deliveries(id,user_id,discord_id,payload,created) VALUES(?,?,?,?,?)',
                (ident,user_id,discord_id,payload,time.time()))
        return ident

    def prepare_delivery(self,user_id,job_id,amount,target,timeout=20):
        ident=self._queue_notice(user_id,'prepare:'+user_id+':'+job_id,{'kind':'prepare','amount':str(amount),'target':target})
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            with self.store.conn() as c:row=c.execute('SELECT status,channel_id,message_id FROM discord_deliveries WHERE id=?',(ident,)).fetchone()
            if row['status']=='sent' and row['channel_id'] and row['message_id']:return dict(row)
            if row['status'] in {'failed','unknown','cancelled'}:break
            time.sleep(.2)
        with self.store.conn() as c:
            c.execute("UPDATE discord_deliveries SET status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE id=?",(ident,))
        raise ValueError('Discord-privébericht niet afgeleverd. Zet privéberichten aan. Er is niets geboekt.')

    def close_prepared(self,user_id,job_id,prepared,*,confirmed=False):
        self._queue_notice(user_id,'notice:'+user_id+':'+job_id,{
            'kind':'notice','confirmed':confirmed,'channel_id':prepared['channel_id'],'message_id':prepared['message_id']})

    def queue_payment(self,user_id,sale_id,url,amount,target,prepared=None):
        from .payment import build_payment_url
        url=build_payment_url({'userInitiatedPaymentResult':{'bSuccess':True,'hostedPaymentPageUrl':url}})
        if not url:raise ValueError('Geen geldige betaallink ontvangen.')
        content={'kind':'payment','url':url,'amount':str(amount),'target':target}
        if prepared:content.update(channel_id=prepared['channel_id'],message_id=prepared['message_id'])
        self._queue_notice(user_id,f'payment:{user_id}:{int(sale_id)}',content)

    def delivery_status(self,user_id):
        with self.store.conn() as c:
            rows=c.execute('SELECT status,error,created FROM discord_deliveries WHERE user_id=? ORDER BY created DESC LIMIT 5',(user_id,)).fetchall()
        return [dict(r) for r in rows]
