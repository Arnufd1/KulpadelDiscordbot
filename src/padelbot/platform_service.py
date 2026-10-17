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

from .web_store import PlatformStore,digest,google_return_path
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


def usable_local_card(card):
    return bool(card and not card.get('expired',False)
                and card.get('network') in {'visa','mastercard','bancontact','maestro'})


def validate_payment(me,amount,mode,*,local_card=None):
    if mode=='discord':return
    if mode!='kul':raise ValueError('Kies een betaalwijze.')
    try:
        balance=Decimal(str(me.get('balance') or 0))
        if balance.is_finite() and balance>=amount:return
    except (ValueError,TypeError,InvalidOperation):pass
    if usable_local_card(local_card):return
    raise ValueError('Voeg een betaalkaart toe of kies een betaallink.')


CARD_MANAGE_URL='/#settings'
AUTOBOOK_CARD_REQUIRED='Voeg eerst een betaalkaart toe voor autobooking.'


def validate_autobook_payment(me,mode,*,local_card=None):
    """Autobooking needs an unexpired local card even when balance covers a sale."""
    if mode!='kul' or not usable_local_card(local_card):
        raise ValueError(AUTOBOOK_CARD_REQUIRED)


def public_job_error(error):
    """Activity is a human summary, never a second copy of provider diagnostics."""
    value=str(error or '').lower()
    if 'kaart' in value or 'betaalmethode' in value:return 'Controleer je betaalkaart bij Instellingen.'
    if 'prijs' in value or 'maximum' in value:return 'De prijs past niet binnen je ingestelde maximum.'
    if 'niet meer vrij' in value or 'niet meer beschikbaar' in value:return 'Dit tijdstip is niet meer vrij.'
    if 'al een boeking' in value:return 'Je hebt al een boeking op dit tijdstip.'
    if 'gepauzeerd' in value:return 'Je profiel is gepauzeerd.'
    if 'pin' in value or 'authenticator' in value or 'koppel eerst' in value:return 'Controleer je KUL-koppeling.'
    if 'instellingen zijn gewijzigd' in value or 'betaalinstellingen' in value:return 'Je instellingen zijn gewijzigd. Er is niets geboekt.'
    if 'lang' in value or 'onderbroken' in value or 'herstart' in value:return 'De controle werd onderbroken. Controleer je boekingen.'
    if 'wachtrij' in value or 'taken klaar' in value:return 'Er staan al taken klaar. Wacht even.'
    return 'KUL kon de controle niet afronden. Controleer je koppeling en boekingen.'


class PlatformService:
    def __init__(self,store:PlatformStore,community=None,*,cards=None):
        self.store=store
        self.cards=cards
        self.community=community or CommunityBookings(store)
        self.hub=self.community.hub
        with store.conn() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS discord_oauth (
                    state TEXT PRIMARY KEY,session TEXT NOT NULL,user_id TEXT NOT NULL,expires REAL NOT NULL,
                    return_to TEXT NOT NULL DEFAULT '/#dashboard');
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
            if 'return_to' not in {r[1] for r in c.execute('PRAGMA table_info(discord_oauth)')}:
                c.execute("ALTER TABLE discord_oauth ADD COLUMN return_to TEXT NOT NULL DEFAULT '/#dashboard'")
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
        if kind not in {'slots','quote','book','bookings','login','account','rule','community_join','cancel_booking'}:
            raise ValueError('Onbekende taak.')
        if kind in {'book','rule','community_join'}:self.require_writable(user['id'])
        if kind=='account':return self.account_check(user,force=True)['check']['job_id']
        if kind=='rule':
            if not self.can_autobook(user['id']):raise ValueError(AUTOBOOK_CARD_REQUIRED)
        dedup=None
        if kind=='cancel_booking':
            if self.access_status(user['id'])['suspended']:raise ValueError('Dit profiel is gepauzeerd.')
            booking_id=payload.get('booking_id')
            if type(booking_id) is not int or not 0<booking_id<2**63:raise ValueError('Ongeldige reservatie.')
            dedup=f"cancel:{user['id']}:{booking_id}"
            # Repeated clicks and interrupted writes keep the same operation.
            # Ownership is also verified against KUL immediately before writing.
            with self.store.conn() as c:
                prior=c.execute('SELECT id FROM jobs WHERE user_id=? AND dedup=?',(user['id'],dedup)).fetchone()
            if prior:return prior['id']
            if not self._owns_upcoming_booking(user['id'],booking_id):
                raise ValueError('Vernieuw je eigen boekingen voordat je deze reservatie annuleert.')
            payload={'booking_id':booking_id}
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

    def oauth_start(self,user_id,session,return_to='/#dashboard'):
        state=secrets.token_urlsafe(32)
        destination=google_return_path(return_to)
        if destination=='/':destination='/#dashboard'
        with self.store.conn() as c:
            c.execute('DELETE FROM discord_oauth WHERE expires<?',(time.time(),))
            c.execute('INSERT INTO discord_oauth(state,session,user_id,expires,return_to) VALUES(?,?,?,?,?)',
                (digest(state),digest(session),user_id,time.time()+600,destination))
        return state

    def oauth_consume(self,state,session,user_id):
        with self.store.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM discord_oauth WHERE state=? AND session=? AND user_id=? AND expires>?',
                (digest(state),digest(session),user_id,time.time())).fetchone()
            if row:c.execute('DELETE FROM discord_oauth WHERE state=?',(digest(state),))
        return dict(row) if row else None

    def remember_payment(self,user_id,summary,*,checked=None):
        checked=time.time() if checked is None else float(checked)
        with self.store.conn() as c:
            c.execute('INSERT INTO payment_accounts(user_id,balance,direct_debit,checked,stored_method,stored_card) VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET balance=excluded.balance,direct_debit=excluded.direct_debit,checked=excluded.checked,stored_method=excluded.stored_method,stored_card=excluded.stored_card WHERE excluded.checked>=payment_accounts.checked',
                (user_id,str(summary['balance']),int(summary['direct_debit']),checked,int(summary.get('stored_method',False)),int(summary.get('stored_card',False))))

    def _owns_upcoming_booking(self,user_id,booking_id):
        """Use only the signed-in profile's private snapshot, never a guest roster."""
        from .web_community import timestamp
        with self.store.conn() as c:
            rows=c.execute("SELECT result FROM jobs WHERE user_id=? AND kind IN ('bookings','cancel_booking') AND status='done' AND result IS NOT NULL ORDER BY COALESCE(finished,created) DESC LIMIT 100",(user_id,)).fetchall()
            for row in rows:
                result=json.loads(row['result'])
                if 'bookings' not in result:continue
                for booking in result['bookings']:
                    try:
                        if (type(booking.get('id')) is int and booking['id']==booking_id
                                and booking.get('cancellable') is not False and timestamp(booking['start'])>time.time()):return True
                    except (KeyError,ValueError,TypeError):continue
                return False  # A newer complete snapshot supersedes old ownership.
            return bool(c.execute('SELECT 1 FROM community_matches WHERE user_id=? AND booking_id=? AND confirmed=1 AND start_ts>?',
                (user_id,booking_id,time.time())).fetchone())

    def cancellation_status(self,user_id):
        with self.store.conn() as c:
            rows=c.execute("SELECT id,payload,status,created,finished FROM jobs WHERE user_id=? AND kind='cancel_booking' AND dedup IS NOT NULL ORDER BY created DESC",(user_id,)).fetchall()
        result={}
        for row in rows:
            booking_id=str(json.loads(row['payload'])['booking_id'])
            if booking_id in result:continue
            result[booking_id]={'job_id':row['id'],'status':row['status'],'updated':row['finished'] or row['created']}
        return result

    def confirm_cancelled_booking(self,user_id,booking_id):
        """Invalidate exactly one confirmed KUL cancellation, even if refresh failed."""
        booking_id=int(booking_id);now=time.time()
        with self.store.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE community_matches SET confirmed=0,shared=0,verified=? WHERE user_id=? AND booking_id=?',(now,user_id,booking_id))
            c.execute('UPDATE booking_notifications SET confirmed=0,verified=? WHERE user_id=? AND booking_id=?',(now,user_id,booking_id))
            c.execute("UPDATE private_payments SET status='cancelled',payload='' WHERE user_id=? AND booking_id=?",(user_id,booking_id))
            c.execute("UPDATE discord_deliveries SET status='cancelled',payload='',finished=? WHERE user_id=? AND status='queued' AND (id IN (SELECT 'payment:' || user_id || ':' || sale_id FROM private_payments WHERE user_id=? AND booking_id=?) OR id LIKE ? OR id LIKE ?)",
                (now,user_id,user_id,booking_id,f'booked:{user_id}:{booking_id}:%',f'reminder:{user_id}:{booking_id}:%'))
        with self.hub.matches._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            rows=c.execute('SELECT id FROM shared_matches WHERE hub_profile_id=? AND booking_id=?',(user_id,booking_id)).fetchall()
            for row in rows:
                c.execute("UPDATE shared_matches SET status='cancelled',hub_confirmed=0,hub_visible=0,hub_verified=? WHERE id=?",(now,row['id']))
                self.hub.matches.queue_hub_update(c,row['id'])
        self.store.audit(user_id,'booking_cancelled',str(booking_id))

    def filter_stale_bookings(self,user_id,bookings,*,observed):
        with self.store.conn() as c:
            rows=c.execute("SELECT payload,result FROM jobs WHERE user_id=? AND kind='cancel_booking' AND status='done' AND finished>=?",(user_id,observed)).fetchall()
        cancelled={json.loads(row['payload'])['booking_id'] for row in rows if row['result'] and json.loads(row['result']).get('cancelled') is True}
        return [b for b in bookings if b.get('id') not in cancelled]

    def card_vault(self):
        if self.cards is None:
            from .web import WebSettings
            from .web_cards import CardVault
            config=WebSettings()
            # A standalone/custom store must never inherit the production key.
            key=getattr(self.store,'card_vault_key_file',None)
            if key is None and self.store.root.resolve()==config.root.resolve():key=config.card_vault_key_file
            self.cards=CardVault(self.store,key)
        return self.cards

    def local_card(self,user_id):
        try:return self.card_vault().summary(user_id)
        except (ValueError,OSError):return None

    def attach_local_card(self,user_id,payment):
        card=self.local_card(user_id)
        return {**(payment or {}),'local_card':card,
                'stored_card':usable_local_card(card),
                'stored_method':False,'manage_url':CARD_MANAGE_URL}

    def payment_status(self,user_id):
        with self.store.conn() as c:
            row=c.execute('SELECT balance,direct_debit,checked,stored_method,stored_card FROM payment_accounts WHERE user_id=?',(user_id,)).fetchone()
        result=self.attach_local_card(user_id,dict(row) if row else {'balance':None,'checked':None,'direct_debit':False})
        return result if row or result['local_card'] else None

    def account_status(self,user_id):
        with self.store.conn() as c:
            row=c.execute("SELECT id,status,created,finished,result FROM jobs WHERE user_id=? AND kind IN ('account','login') ORDER BY CASE WHEN status IN ('queued','running','waiting_auth') THEN 0 ELSE 1 END,created DESC LIMIT 1",(user_id,)).fetchone()
        check=None
        if row:
            result=json.loads(row['result']) if row['result'] else {}
            stamp=row['finished'] or row['created']
            check={'job_id':row['id'],'status':row['status'],'checked':stamp,
                   'next_check_at':stamp+(30 if row['status']=='done' else 60),
                   'error':public_job_error(result.get('error')) if result.get('error') else None}
        return {'payment':self.payment_status(user_id),'check':check,
                'checking':bool(check and check['status'] in {'queued','running','waiting_auth'})}

    def account_check(self,user,*,force=False,login=False):
        """One bounded read, shared by the website and Discord; polling never enqueues."""
        if not self.store.user(user['id']) or self.store.user(user['id'])['suspended']:
            raise ValueError('Dit profiel is gepauzeerd.')
        with FileLock(str(self.store.root/('account-check-'+user['id']+'.lock')),timeout=15):
            state=self.account_status(user['id']);check=state['check'];now=time.time()
            if check and (state['checking'] or
                          (check['next_check_at']>now and not (login and check['status']=='done')) or
                          (not login and not force and check['status']=='done' and check['checked']>now-300)):
                return state
            self.store.enqueue(user['id'],'login' if login else 'account',{})
            return self.account_status(user['id'])

    def require_autobook_card(self,user_id,*,fresh=True):
        card=self.local_card(user_id)
        if not usable_local_card(card):
            raise ValueError(AUTOBOOK_CARD_REQUIRED)

    def save_preferences(self,user,values):
        if values.get('automatic_payment'):
            self.require_writable(user['id'])
            if payment_mode(values)!='kul':raise ValueError(AUTOBOOK_CARD_REQUIRED)
            self.require_autobook_card(user['id'])
        return self.store.preferences(user['id'],values)

    def can_autobook(self,user_id):
        try:self.require_writable(user_id)
        except ValueError:return False
        prefs=self.store.preferences(user_id)
        if not prefs.get('automatic_payment') or payment_mode(prefs)!='kul':return False
        try:self.require_autobook_card(user_id,fresh=False)
        except ValueError:return False
        return True

    def require_writable(self,user_id):
        user=self.store.assert_writable(user_id)
        self.hub.register(user)
        try:
            with self.hub.matches._connection() as c:
                self.hub.actor(c,user_id,write=True)
        except RuntimeError as error:
            raise ValueError(str(error)) from None

    def access_status(self,user_id):
        user=self.store.user(user_id)
        state={'suspended':bool(not user or user['suspended']),'restricted':bool(user and user.get('restricted'))}
        with self.hub.matches._connection() as c:
            profile=c.execute('SELECT discord_id,suspended FROM hub_profiles WHERE id=?',(user_id,)).fetchone()
            keys=['profile:'+user_id]+(['discord:'+str(profile['discord_id'])] if profile and profile['discord_id'] is not None else [])
            if profile:state['suspended']=state['suspended'] or bool(profile['suspended'])
            for key in keys:
                row=c.execute('SELECT suspended,restricted FROM hub_moderation WHERE actor=?',(key,)).fetchone()
                if row:
                    state['suspended']=state['suspended'] or bool(row['suspended'])
                    state['restricted']=state['restricted'] or bool(row['restricted'])
        return state

    def activity(self,user_id):
        with self.store.conn() as c:
            rows=c.execute('SELECT id,kind,status,created,finished,result FROM jobs WHERE user_id=? ORDER BY COALESCE(finished,created) DESC LIMIT 1000',(user_id,)).fetchall()
        groups={}
        for row in rows:
            result=json.loads(row['result']) if row['result'] else {}
            if row['status'] in {'queued','running','waiting_auth'}:continue
            failed=row['status'] in {'failed','unknown','cancelled'}
            error=public_job_error(result.get('error')) if failed else None
            # Repeated account checks and the same failed retries occupy one line.
            key=(('error',error) if failed else
                 ('check' if row['kind'] in {'account','login'} else row['kind'],
                  'done' if row['kind'] in {'account','login','slots','bookings','quote'} else row['id']))
            if key in groups:
                groups[key]['attempts']+=1
                continue
            message=('Je hebt geboekt.' if row['kind'] in {'book','rule'} and result.get('paid') is True else
                     'Reservatie geannuleerd.' if row['kind']=='cancel_booking' and result.get('cancelled') is True else
                     'Betaling nog niet bevestigd. Controleer je boeking bij KUL.' if result.get('payment_pending') else
                     'Wachten op betaling.' if result.get('payment_available') else
                     'Nog geen terrein vrij.' if result.get('waiting') else None)
            groups[key]={'id':row['id'],'kind':row['kind'],'status':row['status'],
                         'created':row['finished'] or row['created'],'finished':row['finished'],
                         'result':{'error':error} if failed else {'message':message} if message else None,
                         'attempts':1}
        return list(groups.values())[:30]

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
