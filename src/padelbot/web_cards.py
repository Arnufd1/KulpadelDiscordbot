"""Authenticated card encryption. The web API never returns a full card number."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
import secrets
import time
import uuid
from zoneinfo import ZoneInfo

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .web_store import PlatformStore


class CardVault:
    def __init__(self, store: PlatformStore, key_file: Path | None):
        self.store = store
        self.key = key_file.read_bytes() if key_file else None
        if self.key is not None and len(self.key) != 32:
            raise ValueError('De kaartkluis heeft een ongeldige sleutel.')
        with store.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            existing={r['name'] for r in c.execute("PRAGMA table_info(payment_cards)")}
            if existing and 'is_primary' not in existing:
                c.execute('ALTER TABLE payment_cards RENAME TO payment_cards_v1')
            c.execute('''CREATE TABLE IF NOT EXISTS payment_cards (
                user_id TEXT NOT NULL REFERENCES users(id), id TEXT PRIMARY KEY,
                wrapped_key BLOB NOT NULL, wrap_nonce BLOB NOT NULL,
                ciphertext BLOB NOT NULL, nonce BLOB NOT NULL, updated REAL NOT NULL,
                is_primary INTEGER NOT NULL DEFAULT 0)''')
            if existing and 'is_primary' not in existing:
                c.execute('INSERT INTO payment_cards SELECT *,1 FROM payment_cards_v1')
                c.execute('DROP TABLE payment_cards_v1')
            c.execute('CREATE UNIQUE INDEX IF NOT EXISTS one_primary_card ON payment_cards(user_id) WHERE is_primary=1')
            c.execute('CREATE TABLE IF NOT EXISTS card_migrations (name TEXT PRIMARY KEY)')

    def _ready(self):
        if self.key is None:
            raise ValueError('Kaartopslag is nog niet ingesteld.')

    @staticmethod
    def _aad(user_id, card_id, purpose):
        return f'sigmadvisor-card-v1:{purpose}:{user_id}:{card_id}'.encode()

    def save(self, user_id: str, *, holder: str, number: str, month: int, year: int, label: str='',primary: bool=False,_migration=None):
        self._ready()
        number = re.sub(r'[ -]', '', number)
        if not re.fullmatch(r'[0-9]{13,19}', number):
            raise ValueError('Vul een geldig kaartnummer in.')
        digits = [int(d) for d in reversed(number)]
        if sum(d if i % 2 == 0 else (2*d-9 if d>4 else 2*d) for i,d in enumerate(digits)) % 10:
            raise ValueError('Controleer het kaartnummer.')
        holder = ' '.join(holder.split())
        if not 1 <= len(holder) <= 80 or any(ord(ch)<32 for ch in holder):
            raise ValueError('Vul de naam van de kaarthouder in.')
        today = datetime.now(ZoneInfo('Europe/Brussels'))
        if not 1<=month<=12 or not today.year<=year<=today.year+20 or (year,month)<(today.year,today.month):
            raise ValueError('Vul een geldige, toekomstige vervaldatum in.')
        card_id = uuid.uuid4().hex
        data_key = AESGCM.generate_key(bit_length=256)
        nonce, wrap_nonce = secrets.token_bytes(12), secrets.token_bytes(12)
        # Labels are returned in plain text by the masked-card endpoint. Check the
        # whole input before truncation, including punctuation and zero-width
        # separators, so a formatted PAN cannot leak through the display name.
        if len(re.findall(r'[0-9]',label))>=13:
            raise ValueError('Gebruik een kaartnaam, geen kaartnummer.')
        label=' '.join(label.split())[:40]
        payload = json.dumps({'holder':holder,'number':number,'month':month,'year':year,'label':label}).encode()
        ciphertext = AESGCM(data_key).encrypt(nonce,payload,self._aad(user_id,card_id,'data'))
        wrapped = AESGCM(self.key).encrypt(wrap_nonce,data_key,self._aad(user_id,card_id,'key'))
        with self.store.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            if _migration and c.execute('SELECT 1 FROM card_migrations WHERE name=?',(_migration,)).fetchone():return None
            count=c.execute('SELECT count(*) FROM payment_cards WHERE user_id=?',(user_id,)).fetchone()[0]
            if count>=5:raise ValueError('Je kunt maximaal vijf kaarten bewaren. Verwijder eerst een kaart.')
            primary=bool(primary or count==0)
            if primary:c.execute('UPDATE payment_cards SET is_primary=0 WHERE user_id=?',(user_id,))
            c.execute('INSERT INTO payment_cards VALUES(?,?,?,?,?,?,?,?)',
                (user_id,card_id,wrapped,wrap_nonce,ciphertext,nonce,time.time(),int(primary)))
            if _migration:c.execute('INSERT INTO card_migrations VALUES(?)',(_migration,))
        self.store.audit(user_id,'card_saved')
        return next(card for card in self.list(user_id) if card['id']==card_id)

    def summary(self, user_id: str):
        cards=self.list(user_id)
        return next((card for card in cards if card['primary']),None)

    def list(self,user_id):
        with self.store.conn() as c:
            rows=c.execute('SELECT * FROM payment_cards WHERE user_id=? ORDER BY is_primary DESC,updated,id',(user_id,)).fetchall()
        return [self._summary(user_id,row) for row in rows]

    def _summary(self,user_id,row):
        self._ready()
        try:
            key = AESGCM(self.key).decrypt(row['wrap_nonce'],row['wrapped_key'],self._aad(user_id,row['id'],'key'))
            data = json.loads(AESGCM(key).decrypt(row['nonce'],row['ciphertext'],self._aad(user_id,row['id'],'data')))
        except (InvalidTag,ValueError,KeyError,TypeError):
            raise ValueError('De bewaarde kaart kon niet veilig worden gelezen. Neem contact op met de beheerder.') from None
        number = data['number']
        brand = 'Visa' if number.startswith('4') else 'American Express' if number[:2] in {'34','37'} else 'Mastercard' if 51<=int(number[:2])<=55 or 2221<=int(number[:4])<=2720 else 'Betaalkaart'
        label=data.get('label')
        # Old encrypted records predate the input guard. Never let those labels
        # bypass the masked-only response contract either.
        if not isinstance(label,str) or len(re.findall(r'[0-9]',label))>=13:
            label=''
        return {'id':row['id'],'brand':brand,'label':label or brand,'last4':number[-4:],
                'month':data['month'],'year':data['year'],'primary':bool(row['is_primary'])}

    def delete(self, user_id: str,card_id=None):
        with self.store.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            if card_id is None and c.execute('SELECT count(*) FROM payment_cards WHERE user_id=?',(user_id,)).fetchone()[0]>1:
                raise ValueError('Selecteer de kaart die je wilt verwijderen.')
            c.execute('PRAGMA secure_delete=ON')
            c.execute('DELETE FROM payment_cards WHERE user_id=?'+(' AND id=?' if card_id else ''),(user_id,card_id) if card_id else (user_id,))
            if not c.execute('SELECT 1 FROM payment_cards WHERE user_id=? AND is_primary=1',(user_id,)).fetchone():
                c.execute('UPDATE payment_cards SET is_primary=1 WHERE id=(SELECT id FROM payment_cards WHERE user_id=? ORDER BY updated,id LIMIT 1)',(user_id,))
        self.store.audit(user_id,'card_deleted')

    def set_primary(self,user_id,card_id):
        with self.store.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM payment_cards WHERE user_id=? AND id=?',(user_id,card_id)).fetchone()
            if not row:raise ValueError('Deze kaart is niet beschikbaar.')
            card=self._summary(user_id,row)
            today=datetime.now(ZoneInfo('Europe/Brussels'))
            if (card['year'],card['month'])<(today.year,today.month):raise ValueError('Deze kaart is vervallen.')
            c.execute('UPDATE payment_cards SET is_primary=0 WHERE user_id=?',(user_id,))
            c.execute('UPDATE payment_cards SET is_primary=1 WHERE user_id=? AND id=?',(user_id,card_id))
        self.store.audit(user_id,'card_primary_changed')

    def import_owner_card(self):
        """One-time explicit legacy migration; CVC is never copied into the new vault."""
        if self.key is None:return
        with self.store.conn() as c:
            if c.execute("SELECT 1 FROM card_migrations WHERE name='owner-revolut-v1'").fetchone():return
            owner=c.execute('SELECT id FROM users WHERE owner=1').fetchone()
        from .config import Settings
        from .card_store import CardStore
        base=Settings()
        if not owner or not base.padelbot_card_file.exists():return
        card=CardStore(base.padelbot_key_file,base.padelbot_card_file).load()
        if card:
            with self.store.conn() as c:empty=not c.execute('SELECT 1 FROM payment_cards WHERE user_id=?',(owner['id'],)).fetchone()
            self.save(owner['id'],holder=card.holder,number=card.number,month=card.exp_month,year=card.exp_year,label='Revolut',primary=empty,_migration='owner-revolut-v1')
            del card
