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
    if mode=='discord':return
    if mode!='kul':raise ValueError('Kies een betaalwijze.')
    try:
        balance=Decimal(str(me.get('balance') or 0))
        if balance.is_finite() and balance>=amount:return
    except (ValueError,TypeError,InvalidOperation):pass
    if me.get('_stored_method',{}).get('type')==3:return
    raise ValueError('Koppel een KUL-betaalmethode of kies een betaallink.')


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
                CREATE TABLE IF NOT EXISTS private_payments (
                    user_id TEXT NOT NULL,sale_id INTEGER NOT NULL,booking_id INTEGER,
                    payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',created REAL NOT NULL,
                    PRIMARY KEY(user_id,sale_id));
                CREATE TABLE IF NOT EXISTS booking_notifications (
                    user_id TEXT NOT NULL,booking_id INTEGER NOT NULL,target TEXT NOT NULL,start_ts REAL NOT NULL,
                    confirmed INTEGER NOT NULL,verified REAL NOT NULL,reminder_queued REAL,
                    PRIMARY KEY(user_id,booking_id));
            ''')
            columns={r[1] for r in c.execute('PRAGMA table_info(discord_deliveries)')}
            for field in ['channel_id','message_id']:
                if field not in columns:c.execute(f'ALTER TABLE discord_deliveries ADD COLUMN {field} INTEGER')
            columns={r[1] for r in c.execute('PRAGMA table_info(payment_accounts)')}
            for field in ['stored_method','stored_card']:
                if field not in columns:c.execute(f'ALTER TABLE payment_accounts ADD COLUMN {field} INTEGER NOT NULL DEFAULT 0')
            columns={r[1] for r in c.execute('PRAGMA table_info(booking_notifications)')}
            if 'reminder_queued' not in columns:c.execute('ALTER TABLE booking_notifications ADD COLUMN reminder_queued REAL')

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

    def queue_payment(self,user_id,sale_id,url,amount,target,prepared=None,*,booking_id=None):
        from .payment import build_payment_url
        url=build_payment_url({'userInitiatedPaymentResult':{'bSuccess':True,'hostedPaymentPageUrl':url}})
        if not url:raise ValueError('Geen geldige betaallink ontvangen.')
        content={'kind':'payment','sale_id':int(sale_id),'url':url,'amount':str(amount),'target':target}
        # The website remains usable without Discord. Checkout tokens are encrypted
        # independently of job/history JSON and are bound to their owning profile.
        private={**content,'user_id':user_id,'sale_id':int(sale_id)}
        encrypted=self.cipher().encrypt(json.dumps(private).encode()).decode()
        with self.store.conn() as c:
            c.execute('INSERT OR IGNORE INTO private_payments(user_id,sale_id,booking_id,payload,created) VALUES(?,?,?,?,?)',
                (user_id,int(sale_id),booking_id,encrypted,time.time()))
        if not self.discord_id(user_id):return False
        if prepared:content.update(channel_id=prepared['channel_id'],message_id=prepared['message_id'])
        self._queue_notice(user_id,f'payment:{user_id}:{int(sale_id)}',content)
        return True

    def private_payments(self,user_id):
        user=self.store.user(user_id)
        if not user or user['suspended']:return []
        now=time.time()
        with self.store.conn() as c:
            rows=c.execute("SELECT * FROM private_payments WHERE user_id=? AND status='pending' AND created>? ORDER BY created DESC LIMIT 20",(user_id,now-3600)).fetchall()
        result=[]
        from .web_community import timestamp
        from .payment import build_payment_url
        for row in rows:
            try:
                value=json.loads(self.cipher().decrypt(row['payload'].encode()))
                if value['user_id']!=user_id or value['sale_id']!=row['sale_id'] or timestamp(value['target'])<=now:continue
                url=build_payment_url({'userInitiatedPaymentResult':{'bSuccess':True,'hostedPaymentPageUrl':value['url']}})
                if not url:continue
                result.append({'id':str(row['sale_id']),'sale_id':row['sale_id'],'booking_id':row['booking_id'],
                    'url':url,'amount':value['amount'],'target':value['target'],'status':'pending','created':row['created']})
            except Exception:continue  # A damaged or cross-tenant token must never be returned.
        return result

    def queue_confirmed_booking(self,user_id,booking_id,target,amount=None,*,now=None):
        from .web_community import timestamp
        now=time.time() if now is None else now
        start=timestamp(target);booking_id=int(booking_id)
        if booking_id<=0 or start<=now:return
        with self.store.conn() as c:
            c.execute('INSERT INTO booking_notifications(user_id,booking_id,target,start_ts,confirmed,verified) VALUES(?,?,?,?,1,?) ON CONFLICT(user_id,booking_id) DO UPDATE SET target=excluded.target,start_ts=excluded.start_ts,confirmed=1,verified=excluded.verified',
                (user_id,booking_id,target,start,now))
            c.execute("UPDATE private_payments SET status='paid',payload='' WHERE user_id=? AND booking_id=?",(user_id,booking_id))
            # A delayed link must not arrive after KUL has confirmed payment.
            c.execute("UPDATE discord_deliveries SET status='cancelled',payload='' WHERE user_id=? AND id IN (SELECT 'payment:' || user_id || ':' || sale_id FROM private_payments WHERE user_id=? AND booking_id=?) AND status='queued'",
                (user_id,user_id,booking_id))
        if self.discord_id(user_id):
            self._queue_notice(user_id,f'booked:{user_id}:{booking_id}:{start}',{'kind':'booked','target':target,'amount':str(amount) if amount is not None else None})

    def sync_verified_bookings(self,user_id,bookings,*,device_verified):
        """Observe KUL reads; historical bookings get reminders, not new-booking DMs."""
        from .web_community import timestamp
        now=time.time();completed=[]
        with self.store.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            pending=c.execute("SELECT sale_id,booking_id FROM private_payments WHERE user_id=? AND status='pending'",(user_id,)).fetchall()
            by_booking={r['booking_id'] for r in pending if r['booking_id']}
            by_sale={r['sale_id'] for r in pending}
            # Invalidation and the complete snapshot replacement are one write:
            # delivery cannot observe a temporary cancelled state mid-refresh.
            c.execute('UPDATE booking_notifications SET confirmed=0 WHERE user_id=?',(user_id,))
            for booking in bookings if device_verified else []:
                if booking.get('confirmed') is not True:continue
                try:
                    bid=int(booking['id']);start=timestamp(booking['start'])
                    if bid<=0 or start<=now:continue
                except (ValueError,TypeError,KeyError):continue
                sale=booking.get('sale_id')
                if bid in by_booking or sale in by_sale:
                    if sale in by_sale:
                        c.execute('UPDATE private_payments SET booking_id=? WHERE user_id=? AND sale_id=? AND booking_id IS NULL',(bid,user_id,sale))
                    completed.append((bid,booking['start']))
                c.execute('INSERT INTO booking_notifications(user_id,booking_id,target,start_ts,confirmed,verified) VALUES(?,?,?,?,1,?) ON CONFLICT(user_id,booking_id) DO UPDATE SET target=excluded.target,start_ts=excluded.start_ts,confirmed=1,verified=excluded.verified',
                    (user_id,bid,booking['start'],start,now))
        for bid,target in completed:self.queue_confirmed_booking(user_id,bid,target,now=now)

    def queue_due_reminders(self,*,now=None):
        now=time.time() if now is None else now
        with self.store.conn() as c:
            rows=c.execute('SELECT * FROM booking_notifications WHERE confirmed=1 AND verified>=? AND start_ts>? AND start_ts<=? AND (reminder_queued IS NULL OR reminder_queued!=start_ts)',(now-300,now,now+1800)).fetchall()
        for row in rows:
            user=self.store.user(row['user_id'])
            if user and not user['suspended'] and self.discord_id(user['id']):
                self._queue_notice(user['id'],f"reminder:{user['id']}:{row['booking_id']}:{row['start_ts']}",{'kind':'reminder','booking_id':row['booking_id'],'target':row['target']})
                with self.store.conn() as c:
                    c.execute('UPDATE booking_notifications SET reminder_queued=? WHERE user_id=? AND booking_id=? AND start_ts=?',
                        (row['start_ts'],user['id'],row['booking_id'],row['start_ts']))

    def users_needing_booking_refresh(self,*,now=None):
        """Read-only candidates; the scheduler supplies its own polling limits."""
        now=time.time() if now is None else now
        with self.store.conn() as c:
            rows=c.execute('''SELECT user_id FROM booking_notifications
                WHERE confirmed=1 AND start_ts>? AND start_ts<=? AND verified<? AND (reminder_queued IS NULL OR reminder_queued!=start_ts)
                UNION SELECT user_id FROM private_payments WHERE status='pending' AND created>?
                UNION SELECT user_id FROM community_matches AS cm WHERE confirmed=1 AND start_ts>? AND start_ts<=?
                    AND NOT EXISTS (SELECT 1 FROM booking_notifications AS bn WHERE bn.user_id=cm.user_id AND bn.booking_id=cm.booking_id AND bn.start_ts=cm.start_ts AND bn.reminder_queued=bn.start_ts)''',
                (now,now+2100,now-120,now-3600,now,now+2100)).fetchall()
        return [r[0] for r in rows]

    def delivery_status(self,user_id):
        with self.store.conn() as c:
            rows=c.execute('SELECT status,error,created FROM discord_deliveries WHERE user_id=? ORDER BY created DESC LIMIT 5',(user_id,)).fetchall()
        return [dict(r) for r in rows]
