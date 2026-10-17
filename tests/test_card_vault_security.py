"""Card input, encrypted storage, internal checkout and masked-output boundaries."""
from datetime import datetime
import json
import secrets
from zoneinfo import ZoneInfo

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from test_web import portal, sign_in
from padelbot.web_cards import CardVault, card_network


CARD={'holder':'Private Holder','number':'4111111111111111','month':12,'year':2035}


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


@pytest.mark.parametrize('extra',['cvc','cvv','CVC','security_code','user_id','id'])
def test_card_api_rejects_extra_fields_without_leaking_body(portal,extra,caplog):
    app,client,store,(_,alice,_)=portal
    app.state.cards.key=AESGCM.generate_key(bit_length=256)
    headers=sign_in(client,store,alice)
    response=client.post('/api/payment-card',json={**CARD,extra:'sensitive-value-987'},headers=headers)
    assert response.status_code==422
    assert CARD['number'] not in response.text+caplog.text
    assert CARD['holder'] not in response.text+caplog.text
    assert 'sensitive-value-987' not in response.text+caplog.text
    assert app.state.cards.list(alice['id'])==[]


@pytest.mark.parametrize('change',[
    {'number':'4111111111111112'}, {'number':['4111111111111111']},
    {'month':True}, {'year':'2035'}, {'primary':'true'},
    {'funding':'Mastercard'}, {'holder':'4111.1111.1111.1111'},
    {'label':'4111.1111.1111.1111'}, {'month':0}, {'year':2020},
])
def test_invalid_card_payload_has_only_safe_validation_errors(portal,change,caplog):
    app,client,store,(_,alice,_)=portal
    app.state.cards.key=AESGCM.generate_key(bit_length=256)
    headers=sign_in(client,store,alice)
    response=client.post('/api/payment-card',json={**CARD,**change},headers=headers)
    assert response.status_code in {400,422}
    assert CARD['number'] not in response.text+caplog.text
    assert CARD['holder'] not in response.text+caplog.text
    assert app.state.cards.list(alice['id'])==[]


def test_card_api_requires_identity_origin_and_csrf(portal):
    app,client,store,(_,alice,_)=portal
    app.state.cards.key=AESGCM.generate_key(bit_length=256)
    assert client.post('/api/payment-card',json=CARD,headers={'Origin':'http://testserver'}).status_code==401
    headers=sign_in(client,store,alice)
    assert client.post('/api/payment-card',json=CARD,headers={'Origin':'http://testserver'}).status_code==403
    assert client.post('/api/payment-card',json=CARD,headers={**headers,'Origin':'https://attacker.test'}).status_code==403
    assert app.state.cards.list(alice['id'])==[]


@pytest.mark.parametrize('column',['wrapped_key','wrap_nonce','ciphertext','nonce'])
def test_vault_tamper_fails_closed_in_api_and_internal_checkout(portal,column):
    app,client,store,(_,alice,_)=portal
    vault=app.state.cards;vault.key=AESGCM.generate_key(bit_length=256)
    card=vault.save(alice['id'],**CARD)
    with store.conn() as connection:
        row=connection.execute('SELECT * FROM payment_cards WHERE id=?',(card['id'],)).fetchone()
        value=row[column]
        connection.execute(f'UPDATE payment_cards SET {column}=? WHERE id=?',
                           (bytes([value[0]^1])+value[1:],card['id']))
    headers=sign_in(client,store,alice)
    response=client.get('/api/payment-card')
    assert response.status_code==400 and CARD['number'] not in response.text
    with pytest.raises(ValueError,match='veilig'):
        vault.load_primary(alice['id'])


def test_envelope_keys_nonces_ciphertext_are_unique_and_database_contains_no_card_data(portal):
    app,_,store,(_,alice,_)=portal
    vault=app.state.cards;vault.key=AESGCM.generate_key(bit_length=256)
    for _ in range(3):vault.save(alice['id'],**CARD,label='Private Label')
    with store.conn() as connection:
        rows=connection.execute('SELECT * FROM payment_cards').fetchall()
    for field in ['nonce','wrap_nonce','wrapped_key','ciphertext']:
        assert len({row[field] for row in rows})==3
    for path in [store.path,store.path.with_name(store.path.name+'-wal')]:
        if path.exists():
            blob=path.read_bytes()
            for sensitive in [CARD['number'],CARD['holder'],'Private Label']:
                assert sensitive.encode() not in blob


def test_missing_or_wrong_key_never_replaces_existing_key_or_decrypts(portal,tmp_path):
    app,_,store,(_,alice,_)=portal
    keyfile=tmp_path/'outside-db.key';keyfile.write_bytes(AESGCM.generate_key(bit_length=256))
    vault=CardVault(store,keyfile);vault.save(alice['id'],**CARD)
    keyfile.unlink()
    with pytest.raises(FileNotFoundError):CardVault(store,keyfile)
    assert not keyfile.exists()
    with pytest.raises(ValueError):CardVault(store,None).load_primary(alice['id'])
    with pytest.raises(ValueError):CardVault(store,None).save(alice['id'],**CARD)
    keyfile.write_bytes(AESGCM.generate_key(bit_length=256))
    with pytest.raises(ValueError,match='veilig'):CardVault(store,keyfile).load_primary(alice['id'])


def test_expired_card_is_visible_but_cannot_be_used_or_made_primary(portal):
    app,_,store,(_,alice,_)=portal
    vault=app.state.cards;vault.key=AESGCM.generate_key(bit_length=256)
    card=vault.save(alice['id'],**CARD)
    with store.conn() as connection:
        row=connection.execute('SELECT * FROM payment_cards WHERE id=?',(card['id'],)).fetchone()
        key=AESGCM(vault.key).decrypt(row['wrap_nonce'],row['wrapped_key'],vault._aad(alice['id'],card['id'],'key'))
        data=json.loads(AESGCM(key).decrypt(row['nonce'],row['ciphertext'],vault._aad(alice['id'],card['id'],'data')))
        data['year']=datetime.now(ZoneInfo('Europe/Brussels')).year-1
        nonce=secrets.token_bytes(12)
        encrypted=AESGCM(key).encrypt(nonce,json.dumps(data).encode(),vault._aad(alice['id'],card['id'],'data'))
        connection.execute('UPDATE payment_cards SET nonce=?,ciphertext=? WHERE id=?',(nonce,encrypted,card['id']))
    assert vault.summary(alice['id'])['expired'] is True
    with pytest.raises(ValueError,match='vervallen'):vault.load_primary(alice['id'])
    with pytest.raises(ValueError,match='vervallen'):vault.set_primary(alice['id'],card['id'])


def test_cross_profile_or_card_id_ciphertext_move_is_not_accepted(portal):
    app,_,store,(_,alice,bob)=portal
    vault=app.state.cards;vault.key=AESGCM.generate_key(bit_length=256)
    card=vault.save(alice['id'],**CARD)
    with store.conn() as connection:
        connection.execute('UPDATE payment_cards SET user_id=? WHERE id=?',(bob['id'],card['id']))
    assert vault.load_primary(alice['id']) is None
    with pytest.raises(ValueError,match='veilig'):vault.load_primary(bob['id'])
    with store.conn() as connection:
        connection.execute('UPDATE payment_cards SET user_id=?,id=? WHERE id=?',(alice['id'],'changed-card-id',card['id']))
    with pytest.raises(ValueError,match='veilig'):vault.load_primary(alice['id'])


@pytest.mark.parametrize('number,network,brand',[
    ('4111111111111111','visa','Visa'),('378282246310005','amex','American Express'),
    ('5555555555554444','mastercard','Mastercard'),('2221000000000009','mastercard','Mastercard'),
    ('2720990000000000','mastercard','Mastercard'),('2220000000000000','card','Betaalkaart'),
    ('2721000000000000','card','Betaalkaart'),('6703444444444449','bancontact','Bancontact'),
    ('6759649826438453','maestro','Maestro'),('6200000000000000','card','Betaalkaart'),
])
def test_network_recognition_has_precise_boundaries_and_never_guesses_funding(number,network,brand):
    assert card_network(number)==(network,brand)
