"""Privacy checks for the existing encrypted, read-only card archive."""
import json
import secrets

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from test_web import portal


@pytest.mark.parametrize('label',[
    '4111.1111.1111.1111',
    '4111\u200b1111\u200b1111\u200b1111',
    'Card 4111/1111/1111/1111',
    'Reserve '+('x'*40)+' 4111111111111111',
])
def test_display_label_cannot_expose_formatted_pan(portal,label):
    app,_,_,(_,alice,_)=portal
    vault=app.state.cards
    vault.key=AESGCM.generate_key(bit_length=256)
    with pytest.raises(ValueError,match='kaartnaam') as error:
        vault.save(alice['id'],holder='Example',number='4111111111111111',
                   month=12,year=2035,label=label)
    assert '4111' not in str(error.value)
    assert vault.list(alice['id'])==[]


@pytest.mark.parametrize('legacy_label',[
    '4111111111111111',
    'Card 4111.1111.1111.1111',
    '4111\u200b1111\u200b1111\u200b1111',
])
def test_existing_encrypted_labels_also_obey_masked_response_contract(portal,legacy_label):
    app,_,store,(_,alice,_)=portal
    vault=app.state.cards
    vault.key=AESGCM.generate_key(bit_length=256)
    card=vault.save(alice['id'],holder='Example',number='4111111111111111',
                    month=12,year=2035,label='Personal')
    # Model a record encrypted by the older version, without bypassing AEAD.
    with store.conn() as connection:
        row=connection.execute('SELECT * FROM payment_cards WHERE id=?',(card['id'],)).fetchone()
        key=AESGCM(vault.key).decrypt(row['wrap_nonce'],row['wrapped_key'],
                                    vault._aad(alice['id'],card['id'],'key'))
        payload=json.loads(AESGCM(key).decrypt(row['nonce'],row['ciphertext'],
                                            vault._aad(alice['id'],card['id'],'data')))
        payload['label']=legacy_label
        nonce=secrets.token_bytes(12)
        ciphertext=AESGCM(key).encrypt(nonce,json.dumps(payload).encode(),
                                       vault._aad(alice['id'],card['id'],'data'))
        connection.execute('UPDATE payment_cards SET nonce=?,ciphertext=? WHERE id=?',
                           (nonce,ciphertext,card['id']))
    summary=vault.summary(alice['id'])
    assert summary['label']=='Visa'
    assert summary['last4']=='1111'
    assert '4111' not in json.dumps(summary)
