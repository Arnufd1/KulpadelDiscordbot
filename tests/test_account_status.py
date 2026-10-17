import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from test_web import portal, sign_in
from padelbot.platform_service import AUTOBOOK_CARD_REQUIRED, PlatformService
from padelbot.web_runtime import JobRunner, tenant_env


def connected(store,user):
    token=Path(tenant_env(store,user)['PADELBOT_TOKEN_FILE'])
    token.parent.mkdir(parents=True,exist_ok=True);token.touch()


def finish(store,ident,status,result,*,ago=0):
    with store.conn() as c:
        c.execute('UPDATE jobs SET status=?,result=?,finished=? WHERE id=?',
                  (status,json.dumps(result),time.time()-ago,ident))


def test_automatic_check_is_private_coalesced_and_polling_does_not_requeue(portal):
    app,client,store,(_,alice,bob)=portal
    assert client.get('/api/account/status').status_code==401
    assert client.post('/api/account/check',json={}).status_code==403
    headers=sign_in(client,store,alice)
    assert client.post('/api/account/check',json={},headers=headers).json()['error']
    with store.conn() as c:assert c.execute('SELECT count(*) FROM jobs').fetchone()[0]==0
    connected(store,alice)
    state=client.post('/api/account/check',json={},headers=headers).json()
    ident=state['check']['job_id'];assert state['checking']
    assert client.post('/api/account/check',json={'force':True},headers=headers).json()['check']['job_id']==ident
    assert app.state.platform.enqueue(alice,'account',{})==ident
    for _ in range(5):assert client.get('/api/account/status').json()['check']['job_id']==ident
    with store.conn() as c:assert c.execute('SELECT count(*) FROM jobs').fetchone()[0]==1
    sign_in(client,store,bob)
    assert client.get('/api/account/status').json()=={'payment':None,'check':None,'checking':False}


def test_automatic_check_reuses_pairing_login_without_launching_second_auth(portal):
    _,client,store,(_,alice,_)=portal
    ident=store.enqueue(alice['id'],'login',{})
    headers=sign_in(client,store,alice)
    state=client.post('/api/account/check',json={},headers=headers).json()
    assert state['check']['job_id']==ident and state['checking']


def test_success_cache_force_and_failure_cooldown_are_bounded(portal):
    app,client,store,(_,alice,_)=portal
    headers=sign_in(client,store,alice);connected(store,alice)
    ident=client.post('/api/account/check',json={},headers=headers).json()['check']['job_id']
    finish(store,ident,'done',{'connected':True},ago=31)
    assert client.post('/api/account/check',json={},headers=headers).json()['check']['job_id']==ident
    newer=client.post('/api/account/check',json={'force':True},headers=headers).json()['check']['job_id']
    assert newer!=ident
    secret='4111111111111111 https://provider.invalid/?token=do-not-expose'
    finish(store,newer,'failed',{'error':secret})
    state=client.post('/api/account/check',json={'force':True},headers=headers).json()
    assert state['check']['job_id']==newer and state['check']['error'] and secret not in json.dumps(state)
    finish(store,newer,'failed',{'error':secret},ago=61)
    assert client.post('/api/account/check',json={'force':True},headers=headers).json()['check']['job_id']!=newer


def test_failure_latch_prevents_automatic_reauthentication(portal):
    _,client,store,(_,alice,_)=portal
    headers=sign_in(client,store,alice);connected(store,alice)
    failure=Path(tenant_env(store,alice)['PADELBOT_ANDROID_FAILURE_FILE']);failure.touch()
    state=client.post('/api/account/check',json={'force':True},headers=headers).json()
    assert state['error'] and not state['checking']
    with store.conn() as c:assert c.execute('SELECT count(*) FROM jobs').fetchone()[0]==0


def test_autobook_requires_fresh_kul_card_and_background_eligibility_does_not_expire(portal):
    app,client,store,(_,alice,bob)=portal;s=app.state.platform
    headers=sign_in(client,store,alice)
    prefs={'automatic_payment':True,'payment_mode':'kul','max_amount':20}
    s.remember_payment(alice['id'],{'balance':100,'direct_debit':False,'stored_card':False})
    assert client.post('/api/preferences',json=prefs,headers=headers).status_code==400
    s.remember_payment(bob['id'],{'balance':100,'direct_debit':False,'stored_card':True})
    assert client.post('/api/preferences',json=prefs,headers=headers).status_code==400
    s.remember_payment(alice['id'],{'balance':100,'direct_debit':False,'stored_card':True})
    assert client.post('/api/preferences',json={**prefs,'payment_mode':'discord'},headers=headers).status_code==400
    assert client.post('/api/preferences',json=prefs,headers=headers).status_code==200
    failed=store.enqueue(alice['id'],'account',{})
    finish(store,failed,'failed',{'error':'KUL unavailable'})
    assert client.post('/api/preferences',json=prefs,headers=headers).status_code==400
    assert store.preferences(alice['id'])==prefs
    with store.conn() as c:c.execute('UPDATE payment_accounts SET checked=? WHERE user_id=?',(time.time()-86400,alice['id']))
    assert s.can_autobook(alice['id'])  # Actual token is rechecked by the worker, not browser presence.
    assert client.post('/api/preferences',json=prefs,headers=headers).status_code==400
    s.remember_payment(alice['id'],{'balance':100,'direct_debit':False,'stored_card':False})
    assert not s.can_autobook(alice['id'])
    with pytest.raises(ValueError,match='kaart'):s.enqueue(alice,'rule',{'rule_id':1,'target':'2030-01-01T18:00:00Z'})


@pytest.mark.parametrize('remove_during_lookup',[False,True])
def test_rule_rechecks_kul_card_before_cart_even_when_balance_covers_price(portal,monkeypatch,remove_during_lookup):
    app,_,store,(_,alice,_)=portal;s=app.state.platform
    from padelbot import web_worker,client,booking,booking_manager,checkout,scheduler
    from padelbot.config import settings
    from padelbot.rules_store import RulesStore
    monkeypatch.setenv('PADELWEB_DB',str(store.path));monkeypatch.setenv('PADELWEB_USER_ID',alice['id'])
    monkeypatch.setattr(web_worker,'initialise_key',lambda:None)
    monkeypatch.setattr(web_worker,'platform_context',lambda:s.community)
    rulefile=store.root/'isolated-rules.sqlite3';monkeypatch.setattr(settings,'padelbot_rules_file',rulefile)
    store.preferences(alice['id'],{'automatic_payment':True,'payment_mode':'kul','max_amount':20})
    target=datetime.now(timezone.utc)+timedelta(days=1)
    slot=booking.Slot(100,200,target.isoformat(),(target+timedelta(hours=1)).isoformat(),12,{})
    rule=RulesStore(rulefile).add_rule(day_of_week=target.weekday(),time_local='18:00')
    reads=[]
    class Client:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def me(self):return {'id':42,'balance':100}
        def get(self,path,params):
            reads.append(path)
            card={'id':10,'personId':42,'type':3,'paymentProviderType':8,'preferred':True,'expirationDate':'2035-12-01'}
            return {'data':[card] if remove_during_lookup and len(reads)==1 else []}
    monkeypatch.setattr(client,'BackboneClient',Client)
    monkeypatch.setattr(booking,'find_slots',lambda *a,**kw:[slot])
    monkeypatch.setattr(scheduler,'find_rule_slot',lambda *a,**kw:slot)
    monkeypatch.setattr(checkout,'slot_price',lambda *a:Decimal(12))
    monkeypatch.setattr(booking_manager,'list_my_bookings',lambda *a,**kw:[])
    monkeypatch.setattr(booking,'book',lambda *a,**kw:pytest.fail('No reservation without a current KUL card'))
    with pytest.raises(ValueError,match='kaart'):
        web_worker.execute('rule',{'rule_id':rule.id,'target':target.isoformat()})
    assert len(reads)==(2 if remove_during_lookup else 1)
    assert s.payment_status(alice['id'])['stored_card']==0 and not s.can_autobook(alice['id'])


def test_activity_groups_retries_uses_latest_time_and_never_exposes_raw_result(portal):
    _,client,store,(_,alice,bob)=portal
    headers=sign_in(client,store,alice)
    for n in range(40):
        ident=store.enqueue(alice['id'],['account','slots','bookings'][n%3],{})
        finish(store,ident,'failed',{'error':'provider failed 4111111111111111 token=secret'+str(n)},ago=40-n)
    other=store.enqueue(bob['id'],'account',{});finish(store,other,'failed',{'error':'private other user'})
    ident=store.enqueue(alice['id'],'book',{})
    finish(store,ident,'done',{'paid':True,'payment_url':'https://provider.invalid/secret','raw_card':'4111111111111111'})
    ident=store.enqueue(alice['id'],'book',{})
    finish(store,ident,'done',{'paid':True})
    history=client.get('/api/history').json()['jobs']
    assert len(history)==3
    failed=next(item for item in history if item['status']=='failed')
    assert failed['attempts']==40 and failed['created']>time.time()-5
    assert failed['created']==failed['finished']
    text=json.dumps(history)
    assert all(value not in text for value in ['4111111111111111','token=','provider.invalid','other user','raw_card'])
    with store.conn() as c:assert c.execute('SELECT count(*) FROM jobs').fetchone()[0]==43  # Audit is preserved.
