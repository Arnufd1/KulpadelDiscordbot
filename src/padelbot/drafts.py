"""Unpaid proposals with finite cart checks; creating a draft never creates a sale."""
from __future__ import annotations
import secrets,time,uuid
from datetime import datetime,timezone
from .cart import own_cart,_key,_slot_key,_reusable,remove_own_item,CartStateUnknownError

PERIOD=45*60


def draft_deadline(created,start,now):
    def cap(instant):
        ahead=start-instant
        return 24*3600 if ahead>48*3600 else 6*3600 if ahead>16*3600 else PERIOD
    limit=min(created+cap(created),created+cap(now),start)
    return created+max(0,int((limit-created)//PERIOD))*PERIOD


def ensure_draft_cart(client,slot,member,category,label):
    """Never replace another basket item or force-reset a provider timeout."""
    from .booking import _slot_params,SITE_ID
    exact=[item for item in own_cart(client,member) if _key(item)==_slot_key(slot)]
    if exact:
        if len(exact)==1 and _reusable(exact[0],category,label):return exact[0]
        raise ValueError('Dit tijdslot staat al in je KUL-winkelwagen. Het bestaande item is niet gewijzigd.')
    body={'type':'product','productId':slot.bookable_product_id,'amount':1,'params':_slot_params(slot,label=label),
          'category':category,'siteId':SITE_ID,'memberId':member}
    try:client.post('/cart-items',body)
    except Exception:
        # A response can be lost after the write. Only a read can confirm it.
        found=[i for i in own_cart(client,member) if _key(i)==_slot_key(slot) and _reusable(i,category,label)]
        if len(found)==1:return found[0]
        raise CartStateUnknownError('Het draft-item is niet bevestigd. Er is geen betaling gestart.') from None
    found=[i for i in own_cart(client,member) if _key(i)==_slot_key(slot) and _reusable(i,category,label)]
    if len(found)!=1:raise CartStateUnknownError('Het draft-item is niet bevestigd. Controleer de KUL-winkelwagen.')
    return found[0]


class DraftStore:
    def __init__(self,community):
        self.community=community
        self.matches=community.matches
        with self.matches._connection() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS hub_drafts (
                id TEXT PRIMARY KEY REFERENCES shared_matches(id),profile_id TEXT NOT NULL,
                product INTEGER NOT NULL,court INTEGER NOT NULL,target TEXT NOT NULL,end TEXT NOT NULL,
                created REAL NOT NULL,deadline REAL NOT NULL,next_check REAL NOT NULL,
                state TEXT NOT NULL,cart_id INTEGER,checked REAL,error TEXT,
                stop_reason TEXT NOT NULL DEFAULT 'expired',cleanup_attempts INTEGER NOT NULL DEFAULT 0,
                UNIQUE(profile_id,product,court,target))''')

    def get(self,draft_id):
        with self.matches._connection() as c:
            row=c.execute('SELECT * FROM hub_drafts WHERE id=?',(draft_id,)).fetchone()
        if not row:raise ValueError('Draft niet gevonden.')
        return dict(row)

    def owned(self,draft_id,profile_id):
        row=self.get(draft_id)
        if row['profile_id']!=profile_id:raise ValueError('Deze draft hoort bij een ander profiel.')
        return row

    def create(self,profile_id,product,court,target,end):
        self.community.store.assert_writable(profile_id)
        from .web_community import timestamp
        start=timestamp(target);finish=timestamp(end);now=time.time()
        target=datetime.fromtimestamp(start,timezone.utc).isoformat()
        end=datetime.fromtimestamp(finish,timezone.utc).isoformat()
        if finish<=start or start>now+7*86400:raise ValueError('Kies een geldig padelslot binnen zeven dagen.')
        deadline=draft_deadline(now,start,now)
        if deadline<=now:raise ValueError('Dit slot begint te snel voor een draft van 45 minuten.')
        with self.matches._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT * FROM hub_drafts WHERE profile_id=? AND product=? AND court=? AND target=?',(profile_id,product,court,target)).fetchone()
            if old:raise ValueError('Voor dit tijdslot bestaat al een draft. De maximale looptijd wordt niet opnieuw gestart.')
            if c.execute("SELECT count(*) FROM hub_drafts WHERE profile_id=? AND state IN ('pending','active','refreshing','uncertain','converting','payment_pending')",(profile_id,)).fetchone()[0]>=3:
                raise ValueError('Je kunt maximaal drie actieve drafts hebben.')
            actor=self.community.hub.actor(c,profile_id,write=True)
            match_id=uuid.uuid4().hex
            c.execute('''INSERT INTO shared_matches(id,owner_id,booking_id,start_iso,end_iso,start_at,court,channel_id,
                status,hub_profile_id,hub_visible,hub_confirmed) VALUES(?,?,?,?,?,?,?,0,'draft',?,0,0)''',
                (match_id,actor,-secrets.randbelow(2**62)-1,target,end,start,'Padel',profile_id))
            c.execute('INSERT INTO match_players VALUES(?,?,?)',(match_id,actor,now))
            c.execute('INSERT INTO hub_drafts(id,profile_id,product,court,target,end,created,deadline,next_check,state) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (match_id,profile_id,product,court,target,end,now,deadline,now,'pending'))
        return self.get(match_id)

    def activate(self,draft_id,slot,item,name):
        row=self.get(draft_id)
        now=time.time()
        from .web_community import timestamp
        deadline=min(row['deadline'],draft_deadline(row['created'],timestamp(row['target']),now))
        with self.matches._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            current=c.execute('SELECT state FROM hub_drafts WHERE id=?',(draft_id,)).fetchone()
            if not current or current['state'] not in {'pending','active','refreshing'}:raise ValueError('Deze draft is gestopt.')
            if row['state'] not in {'pending','active','refreshing'} or now>=deadline:
                raise ValueError('Deze draft is afgelopen of gestopt.')
            next_check=row['created']+(int((now-row['created'])//PERIOD)+1)*PERIOD
            c.execute("UPDATE hub_drafts SET state='active',cart_id=?,checked=?,end=?,deadline=?,next_check=?,error=NULL WHERE id=?",
                      (item['id'],now,slot.end_iso,deadline,next_check,draft_id))
            c.execute('UPDATE shared_matches SET court=?,end_iso=?,hub_visible=1,hub_verified=? WHERE id=?',(name,slot.end_iso,now,draft_id))

    def stop(self,draft_id,profile_id,reason='cancelled'):
        row=self.owned(draft_id,profile_id)
        if row['state'] in {'confirmed','converting','payment_pending'}:raise ValueError('Controleer deze echte boeking eerst bij KUL.')
        with self.matches._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            current=c.execute('SELECT state FROM hub_drafts WHERE id=?',(draft_id,)).fetchone()
            if current['state'] in {'confirmed','converting','payment_pending'}:raise ValueError('Controleer deze echte boeking eerst bij KUL.')
            c.execute("UPDATE hub_drafts SET state='stopping',stop_reason=?,next_check=0 WHERE id=?",(reason,draft_id))
            c.execute('UPDATE shared_matches SET hub_visible=0 WHERE id=?',(draft_id,))

    def failed(self,draft_id,error):
        with self.matches._connection() as c:
            c.execute("UPDATE hub_drafts SET state='uncertain',error=? WHERE id=? AND state IN ('pending','active','refreshing')",(error,draft_id))
            if c.execute('SELECT changes()').fetchone()[0]:c.execute('UPDATE shared_matches SET hub_visible=0 WHERE id=?',(draft_id,))

    def due(self):
        from .web_community import timestamp
        now=time.time();result=[]
        with self.matches._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            rows=c.execute("SELECT * FROM hub_drafts WHERE state IN ('active','uncertain','stopping')").fetchall()
            for row in rows:
                user=self.community.store.user(row['profile_id'])
                deadline=min(row['deadline'],draft_deadline(row['created'],timestamp(row['target']),now))
                stopping=now>=deadline or row['state']=='stopping' or not user or user['suspended']
                c.execute('UPDATE hub_drafts SET deadline=? WHERE id=?',(deadline,row['id']))
                if stopping:
                    c.execute('UPDATE shared_matches SET hub_visible=0 WHERE id=?',(row['id'],))
                    if row['state']=='stopping' and row['next_check']>now:continue
                    if row['cleanup_attempts']>=3:
                        c.execute("UPDATE hub_drafts SET state='cleanup_failed',error='Controleer de KUL-winkelwagen; opruimen kon niet bevestigd worden.' WHERE id=?",(row['id'],))
                        continue
                    c.execute("UPDATE hub_drafts SET state='stopping',cleanup_attempts=cleanup_attempts+1,next_check=? WHERE id=?",(now+300,row['id']))
                    kind='draft_release'
                elif row['state']=='active' and row['next_check']<=now:
                    c.execute("UPDATE hub_drafts SET state='refreshing',next_check=? WHERE id=?",(now+PERIOD,row['id']))
                    kind='draft_refresh'
                else:continue
                result.append({'id':row['id'],'profile_id':row['profile_id'],'kind':kind})
        return result

    def release(self,client,draft_id,member):
        from .booking import Slot
        row=self.get(draft_id)
        if row['state'] in {'confirmed','converting','payment_pending'}:raise ValueError('Deze draft wordt als echte boeking verwerkt.')
        slot=Slot(row['product'],row['court'],row['target'],row['end'],None,{})
        label='Sigmadvisor draft '+draft_id
        for item in own_cart(client,member):
            if _key(item)==_slot_key(slot) and (item.get('params') or {}).get('primaryPurchaseMessage')==label:
                remove_own_item(client,item,member,expected_label=label)
        with self.matches._connection() as c:
            c.execute("UPDATE hub_drafts SET state=stop_reason,cart_id=NULL,error=NULL WHERE id=? AND state NOT IN ('confirmed','converting','payment_pending')",(draft_id,))
            if c.execute('SELECT changes()').fetchone()[0]:c.execute('UPDATE shared_matches SET hub_visible=0 WHERE id=?',(draft_id,))

    def recover(self):
        with self.matches._connection() as c:
            c.execute("UPDATE hub_drafts SET state='uncertain',error='Controleer de draft na de serverherstart.' WHERE state IN ('pending','refreshing')")
            c.execute("UPDATE hub_drafts SET state='payment_pending',error='Controleer KUL voor je opnieuw betaalt.' WHERE state='converting'")
            c.execute("UPDATE shared_matches SET hub_visible=0 WHERE id IN (SELECT id FROM hub_drafts WHERE state IN ('uncertain','payment_pending'))")

    def joined(self,draft_id,profile_id):
        self.community.store.assert_writable(profile_id)
        with self.matches._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM hub_drafts WHERE id=?',(draft_id,)).fetchone()
            if not row or row['state'] not in {'active','refreshing'} or time.time()>=min(row['deadline'],draft_deadline(row['created'],datetime.fromisoformat(row['target']).timestamp(),time.time())):raise ValueError('Deze draft is niet meer actief.')
            host=c.execute('SELECT suspended FROM hub_profiles WHERE id=?',(row['profile_id'],)).fetchone()
            if not host or host['suspended']:raise ValueError('Deze draft is niet meer beschikbaar.')
            actor=self.community.hub.actor(c,profile_id,write=True)
            match=self.matches._get(c,draft_id)
            if actor in match.players:return
            if len(match.players)>=4:raise ValueError('Deze draft heeft al vier deelnemers.')
            c.execute('INSERT INTO match_players VALUES(?,?,?)',(draft_id,actor,time.time()))

    def start_conversion(self,draft_id,profile_id):
        self.community.store.assert_writable(profile_id)
        with self.matches._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            self.community.hub.actor(c,profile_id,write=True)
            row=c.execute('SELECT * FROM hub_drafts WHERE id=? AND profile_id=?',(draft_id,profile_id)).fetchone()
            if not row or row['state']!='active' or time.time()>=min(row['deadline'],draft_deadline(row['created'],datetime.fromisoformat(row['target']).timestamp(),time.time())):
                raise ValueError('Deze draft is niet meer actief of hoort bij een ander profiel.')
            c.execute("UPDATE hub_drafts SET state='converting' WHERE id=? AND state='active'",(draft_id,))
            if c.execute('SELECT changes()').fetchone()[0]!=1:raise ValueError('Deze draft wordt al verwerkt.')
            c.execute('UPDATE shared_matches SET hub_visible=0 WHERE id=?',(draft_id,))

    def converted(self,draft_id,booking_id,paid):
        with self.matches._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE hub_drafts SET state=?,cart_id=NULL WHERE id=?',('confirmed' if paid else 'payment_pending',draft_id))
            c.execute('UPDATE shared_matches SET booking_id=?,status=?,hub_confirmed=?,hub_verified=?,hub_visible=? WHERE id=?',
                (booking_id,'open' if paid else 'draft',int(paid),time.time(),int(paid),draft_id))
            self.matches.queue_hub_update(c,draft_id)

    def uncertain_conversion(self,draft_id):
        with self.matches._connection() as c:
            c.execute("UPDATE hub_drafts SET state='payment_pending',error='Controleer KUL voor je opnieuw betaalt.' WHERE id=? AND state='converting'",(draft_id,))

    def list(self,profile_id):
        now=time.time();result=[]
        with self.matches._connection() as c:
            actor=self.community.hub.actor(c,profile_id)
            rows=c.execute("SELECT * FROM hub_drafts WHERE state IN ('active','refreshing') AND deadline>? ORDER BY target LIMIT 100",(now,)).fetchall()
            for row in rows:
                if now>=draft_deadline(row['created'],datetime.fromisoformat(row['target']).timestamp(),now):continue
                match=self.matches._get(c,row['id'])
                host=c.execute('SELECT suspended FROM hub_profiles WHERE id=?',(row['profile_id'],)).fetchone()
                if not match or not host or host['suspended'] or match.started:continue
                joined=actor in match.players
                if len(match.players)>=4 and not joined:continue
                result.append({'id':row['id'],'title':match.court,'start':row['target'],'end':row['end'],
                    'host':match.names[match.owner_id],'players':[match.names[p] for p in match.players],
                    'count':len(match.players),'capacity':4,'joined':joined,'organiser':match.owner_id==actor,
                    'verified':row['checked'],'kind':'draft','expires':row['deadline'],'next_check':row['next_check']})
        return result

    def own_status(self,profile_id):
        with self.matches._connection() as c:
            rows=c.execute("SELECT d.*,m.court AS title FROM hub_drafts d JOIN shared_matches m ON m.id=d.id WHERE d.profile_id=? AND m.start_at>? AND d.state!='confirmed' ORDER BY d.target",(profile_id,time.time())).fetchall()
        return [{'id':r['id'],'title':r['title'],'start':r['target'],'state':r['state'],'expires':r['deadline'],'error':r['error']} for r in rows]

    def abort_pending(self,draft_id):
        with self.matches._connection() as c:
            c.execute("UPDATE hub_drafts SET state='cancelled' WHERE id=? AND state='pending' AND cart_id IS NULL",(draft_id,))
