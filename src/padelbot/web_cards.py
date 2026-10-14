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
            c.execute('''CREATE TABLE IF NOT EXISTS payment_cards (
                user_id TEXT PRIMARY KEY REFERENCES users(id), id TEXT UNIQUE NOT NULL,
                wrapped_key BLOB NOT NULL, wrap_nonce BLOB NOT NULL,
                ciphertext BLOB NOT NULL, nonce BLOB NOT NULL, updated REAL NOT NULL)''')

    def _ready(self):
        if self.key is None:
            raise ValueError('Kaartopslag is nog niet ingesteld.')

    @staticmethod
    def _aad(user_id, card_id, purpose):
        return f'sigmadvisor-card-v1:{purpose}:{user_id}:{card_id}'.encode()

    def save(self, user_id: str, *, holder: str, number: str, month: int, year: int):
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
        payload = json.dumps({'holder':holder,'number':number,'month':month,'year':year}).encode()
        ciphertext = AESGCM(data_key).encrypt(nonce,payload,self._aad(user_id,card_id,'data'))
        wrapped = AESGCM(self.key).encrypt(wrap_nonce,data_key,self._aad(user_id,card_id,'key'))
        with self.store.conn() as c:
            c.execute('''INSERT INTO payment_cards VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET id=excluded.id,wrapped_key=excluded.wrapped_key,
                wrap_nonce=excluded.wrap_nonce,ciphertext=excluded.ciphertext,nonce=excluded.nonce,updated=excluded.updated''',
                (user_id,card_id,wrapped,wrap_nonce,ciphertext,nonce,time.time()))
        self.store.audit(user_id,'card_saved')
        return self.summary(user_id)

    def summary(self, user_id: str):
        with self.store.conn() as c:
            row = c.execute('SELECT * FROM payment_cards WHERE user_id=?',(user_id,)).fetchone()
        if not row:
            return None
        self._ready()
        try:
            key = AESGCM(self.key).decrypt(row['wrap_nonce'],row['wrapped_key'],self._aad(user_id,row['id'],'key'))
            data = json.loads(AESGCM(key).decrypt(row['nonce'],row['ciphertext'],self._aad(user_id,row['id'],'data')))
        except (InvalidTag,ValueError,KeyError,TypeError):
            raise ValueError('De bewaarde kaart kon niet veilig worden gelezen. Neem contact op met de beheerder.') from None
        number = data['number']
        brand = 'Visa' if number.startswith('4') else 'American Express' if number[:2] in {'34','37'} else 'Mastercard' if 51<=int(number[:2])<=55 or 2221<=int(number[:4])<=2720 else 'Betaalkaart'
        return {'brand':brand,'last4':number[-4:],'month':data['month'],'year':data['year']}

    def delete(self, user_id: str):
        with self.store.conn() as c:
            c.execute('PRAGMA secure_delete=ON')
            c.execute('DELETE FROM payment_cards WHERE user_id=?',(user_id,))
        self.store.audit(user_id,'card_deleted')
