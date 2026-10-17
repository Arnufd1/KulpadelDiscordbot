import json
from types import SimpleNamespace
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import pytest
from test_web import portal, sign_in
from padelbot.web_cards import CardVault


CARD=dict(holder='Example',number='4111111111111111',month=12,year=2035)


def test_multiple_primary_cards_remain_private_and_deletion_selects_fallback(portal):
    app,client,store,(_,alice,bob)=portal
    app.state.cards.key=AESGCM.generate_key(bit_length=256)
    headers=sign_in(client,store,alice)
    first=app.state.cards.save(alice['id'],**CARD,label='Revolut')['id']
    second=app.state.cards.save(alice['id'],**CARD,label='Reserve',primary=True)['id']
    data=client.get('/api/payment-card').json()
    assert len(data['cards'])==2 and data['card']['id']==second
    headers=sign_in(client,store,bob)
    assert client.post('/api/payment-card/'+first+'/primary',json={},headers=headers).status_code==400
    assert client.delete('/api/payment-card/'+first,headers=headers).status_code==200
    assert client.get('/api/payment-card').json()['cards']==[]
    headers=sign_in(client,store,alice)
    assert client.get('/api/payment-card').json()['card']['id']==second
    assert client.post('/api/payment-card/'+first+'/primary',json={},headers=headers).status_code==200
    assert client.get('/api/payment-card').json()['card']['id']==first
    assert client.delete('/api/payment-card/'+first,headers=headers).status_code==200
    assert client.get('/api/payment-card').json()['card']['id']==second
    assert sum(c['primary'] for c in client.get('/api/payment-card').json()['cards'])==1


def test_v1_card_migration_preserves_ciphertext_and_primary(portal,tmp_path):
    app,_,store,(_,alice,_)=portal
    key=AESGCM.generate_key(bit_length=256)
    app.state.cards.key=key
    card=app.state.cards.save(alice['id'],**CARD)
    with store.conn() as c:
        c.execute('DROP INDEX one_primary_card')
        c.execute('ALTER TABLE payment_cards RENAME TO migrated_cards')
        c.execute('CREATE TABLE payment_cards(user_id TEXT PRIMARY KEY,id TEXT,wrapped_key BLOB,wrap_nonce BLOB,ciphertext BLOB,nonce BLOB,updated REAL)')
        c.execute('INSERT INTO payment_cards SELECT user_id,id,wrapped_key,wrap_nonce,ciphertext,nonce,updated FROM migrated_cards')
        before=c.execute('SELECT ciphertext FROM payment_cards').fetchone()[0]
        c.execute('DROP TABLE migrated_cards')
    keyfile=tmp_path/'vault.key';keyfile.write_bytes(key)
    vault=CardVault(store,keyfile)
    assert vault.summary(alice['id'])==card
    with store.conn() as c:assert c.execute('SELECT ciphertext FROM payment_cards').fetchone()[0]==before


def test_legacy_revolut_import_is_one_time_and_excludes_cvc(portal,tmp_path,monkeypatch):
    app,_,store,(owner,_,_)=portal
    key=AESGCM.generate_key(bit_length=256)
    app.state.cards.key=key
    legacy=tmp_path/'legacy-card';legacy.touch()
    monkeypatch.setattr('padelbot.config.Settings',lambda:SimpleNamespace(padelbot_card_file=legacy,padelbot_key_file=tmp_path/'key'))
    monkeypatch.setattr('padelbot.card_store.CardStore.load',lambda _:SimpleNamespace(holder='Example',number=CARD['number'],exp_month=12,exp_year=2035,cvv='987'))
    app.state.cards.import_owner_card();app.state.cards.import_owner_card()
    cards=app.state.cards.list(owner['id'])
    assert len(cards)==1 and cards[0]['label']=='Revolut' and cards[0]['primary']
    with store.conn() as c:row=c.execute('SELECT * FROM payment_cards').fetchone()
    aad=app.state.cards._aad
    data_key=AESGCM(key).decrypt(row['wrap_nonce'],row['wrapped_key'],aad(owner['id'],row['id'],'key'))
    payload=json.loads(AESGCM(data_key).decrypt(row['nonce'],row['ciphertext'],aad(owner['id'],row['id'],'data')))
    assert set(payload)=={'holder','number','month','year','label','funding'}
    assert payload['funding']=='unknown'
    assert '987' not in json.dumps(payload)


def test_card_limit_and_label_never_echo_pan(portal):
    app,_,_,(_,alice,_)=portal
    vault=app.state.cards;vault.key=AESGCM.generate_key(bit_length=256)
    with pytest.raises(ValueError):vault.save(alice['id'],**CARD,label=CARD['number'])
    for _ in range(5):vault.save(alice['id'],**CARD)
    with pytest.raises(ValueError):vault.save(alice['id'],**CARD,primary=True)
    assert len(vault.list(alice['id']))==5 and sum(c['primary'] for c in vault.list(alice['id']))==1


def test_card_management_api_saves_masks_switches_and_deletes(portal):
    app,client,store,(_,alice,bob)=portal
    app.state.cards.key=AESGCM.generate_key(bit_length=256)
    headers=sign_in(client,store,alice)
    saved=client.post('/api/payment-card',json={**CARD,'label':'Personal','funding':'debit'},headers=headers)
    assert saved.status_code==200
    first=saved.json()['card']
    assert first['primary'] and first['network']=='visa' and first['funding']=='debit' and not first['expired']
    assert first['last4']=='1111' and 'number' not in first and 'holder' not in first
    assert CARD['number'] not in saved.text and CARD['holder'] not in saved.text
    second=client.post('/api/payment-card',json={**CARD,'label':'Reserve','primary':True},headers=headers).json()['card']
    assert second['primary'] and second['funding']=='unknown'
    assert client.get('/api/payment-card').json()['card']['id']==second['id']
    assert client.post('/api/payment-card/'+first['id']+'/primary',json={},headers=headers).status_code==200
    assert client.get('/api/payment-card').json()['card']['id']==first['id']
    sign_in(client,store,bob)
    assert client.get('/api/payment-card').json()['cards']==[]
    assert app.state.cards.load_primary(bob['id']) is None
    headers=sign_in(client,store,alice)
    assert client.delete('/api/payment-card/'+first['id'],headers=headers).status_code==200
    assert client.get('/api/payment-card').json()['card']['id']==second['id']


def test_vault_internal_checkout_survives_restart_without_cvc(portal,tmp_path):
    app,_,store,(_,alice,bob)=portal
    keyfile=tmp_path/'vault.key';keyfile.write_bytes(AESGCM.generate_key(bit_length=256))
    vault=CardVault(store,keyfile)
    first=vault.save(alice['id'],**CARD)
    restarted=CardVault(store,keyfile)
    assert restarted.summary(alice['id'])==first
    card=restarted.load_primary(alice['id'])
    assert card.holder==CARD['holder'] and card.number==CARD['number']
    assert card.exp_month==12 and card.exp_year==2035 and card.cvv==''
    assert restarted.load_primary(bob['id']) is None
    restarted.delete(bob['id'],first['id'])
    assert restarted.load_primary(alice['id']).number==CARD['number']
    restarted.delete(alice['id'],first['id'])
    assert restarted.load_primary(alice['id']) is None
