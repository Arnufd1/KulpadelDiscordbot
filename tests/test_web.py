import asyncio
import json
import time

import pytest

for optional in ['fastapi','jwt','PIL','zxingcpp']:
    pytest.importorskip(optional,reason='Install .[web,dev] to test the web portal')

from fastapi.testclient import TestClient

from padelbot.web import WebSettings,create_app,validate_google_claims
from padelbot.web_runtime import tenant_env,tenant_root,rules_for
from padelbot.web_store import PlatformStore


@pytest.fixture
def portal(tmp_path,monkeypatch):
    for setting in ['KEY_FILE','TOKEN_FILE','STORAGE_FILE','CARD_FILE','RULES_FILE','ANDROID_PROFILE_FILE','ANDROID_PIN_FILE','ANDROID_FAILURE_FILE']:
        monkeypatch.setenv('PADELBOT_'+setting,str(tmp_path/'legacy-owner'/setting.lower()))
    config = WebSettings(_env_file=None,root=tmp_path,public_url='http://testserver',secure_cookies=False,
                         tail_proxy_secret='test-proxy-secret')
    app=create_app(config,start_workers=False)
    store=app.state.store
    users=[store.login_google({'sub':sub,'email':email,'email_verified':True,'name':sub}) for sub,email in
           [('owner','aafmichiels@gmail.com'),('alice','alice@example.test'),('bob','bob@example.test')]]
    with TestClient(app) as client:
        yield app,client,store,users


def sign_in(client,store,user):
    token,csrf=store.new_session(user['id'])
    client.cookies.set('padel_session',token)
    return {'Origin':'http://testserver','X-CSRF-Token':csrf}


def test_google_start_is_minimal_and_browser_bound(portal):
    app,client,store,users=portal
    app.state.config.google_client_id='example.apps.googleusercontent.com'
    app.state.config.google_client_secret='never-return-this'
    r=client.get('/auth/google',follow_redirects=False)
    assert r.status_code==302
    assert 'code_challenge_method=S256' in r.headers['location']
    assert 'scope=openid+email+profile' in r.headers['location']
    assert 'never-return-this' not in r.text+r.headers['location']
    assert 'HttpOnly' in r.headers['set-cookie']
    assert client.get('/auth/google/callback?state=forged&code=x').status_code==400


def test_oauth_state_requires_same_browser_and_is_one_use(portal):
    _,_,store,_=portal
    state,browser,_,_=store.oauth_start()
    assert store.oauth_consume(state,'attacker') is None
    assert store.oauth_consume(state,browser)
    assert store.oauth_consume(state,browser) is None


@pytest.mark.parametrize('field,value',[('nonce','wrong'),('iss','https://evil.example'),('aud','other'),('email_verified',False),('sub','')])
def test_invalid_google_identity_is_rejected(field,value):
    claims={'iss':'https://accounts.google.com','aud':'client','nonce':'expected','sub':'123','email':'a@example.test','email_verified':True}
    claims[field]=value
    with pytest.raises(ValueError):
        validate_google_claims(claims,'expected','client')


def test_tenant_cannot_read_another_job_or_modify_another_rule(portal):
    _,client,store,(_,alice,bob)=portal
    headers=sign_in(client,store,alice)
    job=client.post('/api/slots',json={'start':0,'end':5},headers=headers).json()['job_id']
    rule=client.post('/api/rules',json={'day_of_week':2,'time_local':'18:00'},headers=headers).json()
    headers=sign_in(client,store,bob)
    assert client.get('/api/jobs/'+job).status_code==404
    assert client.post(f"/api/rules/{rule['id']}/toggle",json={},headers=headers).status_code==404
    assert client.get('/api/rules').json()=={'rules':[]}
    assert client.get('/api/history').json()=={'jobs':[]}


def test_sessions_csrf_origin_and_logout(portal):
    _,client,store,(_,alice,_)=portal
    assert client.get('/api/me').status_code==401
    headers=sign_in(client,store,alice)
    assert client.post('/api/preferences',json={'max_amount':12},headers={'Origin':'http://evil.test'}).status_code==403
    assert client.post('/api/preferences',json={'max_amount':12},headers={'Origin':'http://testserver'}).status_code==403
    assert client.post('/api/preferences',json={'max_amount':12},headers=headers).status_code==200
    assert client.post('/auth/logout',json={},headers=headers).status_code==200
    assert client.get('/api/me').status_code==401


def test_spoofed_tailscale_headers_never_grant_admin(portal):
    _,client,store,(owner,alice,_)=portal
    headers=sign_in(client,store,owner)
    forged={**headers,'Tailscale-User-Login':owner['email'],'X-Padel-Tail-Secret':'test-proxy-secret'}
    assert client.get('/api/admin/users',headers=forged).status_code==403
    url=client.post('/api/admin/access',json={},headers=headers).json()['url']
    assert client.get(url,headers=forged,follow_redirects=False).status_code==403
    headers=sign_in(client,store,alice)
    assert client.post('/api/admin/access',json={},headers=headers).status_code==403


def test_admin_proof_is_bound_to_google_session_and_cannot_be_replayed(portal):
    _,client,store,(owner,alice,_)=portal
    token,csrf=store.new_session(owner['id'])
    ticket=store.admin_ticket(token)
    assert not store.activate_admin(ticket,token)
    assert not store.verify_admin_ticket(ticket,alice['email'])
    assert store.verify_admin_ticket(ticket,owner['email'])
    other,_=store.new_session(owner['id'])
    assert not store.activate_admin(ticket,other)
    assert store.activate_admin(ticket,token)
    assert not store.activate_admin(ticket,token)
    client.cookies.set('padel_session',token)
    r=client.get('/api/admin/users')
    assert r.status_code==200
    assert len(r.json()['users'])==3
    assert all(set(u)<= {'id','email','name','owner','suspended','created','connected','rules'} for u in r.json()['users'])


def test_explicit_tagged_admin_device_can_confirm_but_public_header_cannot(portal):
    app,client,store,(owner,_,_)=portal
    app.state.config.tail_admin_node_ids='trusted-admin-device'
    token,_=store.new_session(owner['id'])
    ticket=store.admin_ticket(token)
    headers={'X-Padel-Tail-Secret':'test-proxy-secret','X-Padel-Tail-Node':'trusted-admin-device'}
    assert client.get('/admin/verify',params={'ticket':ticket},headers=headers,follow_redirects=False).status_code==403
    with TestClient(app,client=('192.168.0.250',12345)) as private:
        response=private.get('/admin/verify',params={'ticket':ticket},headers=headers,follow_redirects=False)
    assert response.status_code==303
    assert store.activate_admin(ticket,token)


def test_pausing_user_revokes_sessions_and_cancels_queued_tasks(portal):
    _,client,store,(owner,alice,_)=portal
    sign_in(client,store,alice)
    job=store.enqueue(alice['id'],'slots',{'start':0,'end':5})
    store.suspend(owner['id'],alice['id'],True)
    assert client.get('/api/me').status_code==401
    assert store.job(alice['id'],job)['status']=='cancelled'
    with pytest.raises(ValueError):
        store.login_google({'sub':'alice','email':alice['email'],'email_verified':True})


def test_worker_has_no_owner_secrets_or_shared_identity(portal,monkeypatch):
    _,_,store,(_,alice,bob)=portal
    monkeypatch.setenv('DISCORD_BOT_TOKEN','owner-discord-secret')
    monkeypatch.setenv('KUL_CARD_CVV','123')
    a,b=tenant_env(store,alice),tenant_env(store,bob)
    for field in ['PADELBOT_KEY_FILE','PADELBOT_TOKEN_FILE','PADELBOT_STORAGE_FILE','PADELBOT_RULES_FILE','PADELBOT_ANDROID_PROFILE_FILE','PADELBOT_ANDROID_PIN_FILE','PADELBOT_ANDROID_EMULATOR_HOME']:
        assert a[field]!=b[field]
        assert str(tenant_root(store,alice)) in a[field]
    assert a['DISCORD_BOT_TOKEN']==a['KUL_CARD_CVV']==''
    assert a['PADELBOT_ENV_FILE']==''
    assert a['PADELBOT_ANDROID_EMULATOR_NAME']!=b['PADELBOT_ANDROID_EMULATOR_NAME']
    assert a['PADELBOT_EMULATOR_LOCK_FILE']==b['PADELBOT_EMULATOR_LOCK_FILE']
    assert not list(tenant_root(store,alice).glob('*.avd'))


def test_booking_submissions_are_idempotent_and_queue_is_bounded(portal):
    _,client,store,(_,alice,_)=portal
    headers=sign_in(client,store,alice)
    body={'target':'2026-10-19T18:00:00Z','product':100,'court':123,'amount':12}
    a=client.post('/api/book',json=body,headers=headers).json()
    b=client.post('/api/book',json=body,headers=headers).json()
    assert a==b
    for _ in range(2):
        assert client.post('/api/actions/account',json={},headers=headers).status_code==200
    assert client.post('/api/actions/account',json={},headers=headers).status_code==400


def test_invalid_ranges_rules_and_other_users_device_are_rejected(portal):
    _,client,store,(_,alice,_)=portal
    headers=sign_in(client,store,alice)
    assert client.post('/api/slots',json={'start':5,'end':0},headers=headers).status_code==400
    assert client.post('/api/rules',json={'day_of_week':0,'time_local':'25:00'},headers=headers).status_code==400
    for action in ['screen','tap','back']:
        assert client.post('/api/device/control',json={'action':action},headers=headers).status_code==422
    assert client.post('/api/device/control',json={'action':'finish','pin':'7392','checked':True},headers=headers).status_code==422
    assert client.post('/api/device/control',json={'action':'shell'},headers=headers).status_code==422
    assert client.get('/api/actions/arbitrary').status_code==405


def test_chunked_large_requests_are_bounded_before_parsing(portal):
    _,client,store,(_,alice,_)=portal
    headers=sign_in(client,store,alice)
    result=client.post('/api/preferences',content=iter([b' '*9000,b' '*9000]),headers=headers)
    assert result.status_code==413


def test_google_subject_cannot_be_reassigned_to_existing_owner_email(portal):
    _,_,store,(owner,_,_)=portal
    with pytest.raises(ValueError):
        store.login_google({'sub':'imposter','email':owner['email'],'email_verified':True})
    assert store.user(owner['id'])['google_sub']=='owner'


@pytest.mark.parametrize('link',['http://icts.kuleuven.be/apps/authenticator/abc','https://evil.test/apps/authenticator/abc',
                                'https://icts.kuleuven.be.evil.test/apps/authenticator/abc','https://icts.kuleuven.be/apps/authenticator/abc?secret=x'])
def test_registration_links_are_scoped_to_official_app(link):
    from padelbot.web_device import registration_link
    with pytest.raises(ValueError):
        registration_link(link)


def test_registration_qr_is_decoded_in_memory_and_external_links_rejected():
    import base64,io
    from PIL import Image
    import zxingcpp
    from padelbot.web_device import decode_registration_qr
    for link in ['https://icts.kuleuven.be/apps/authenticator/ExampleTestData','https://evil.test/phishing']:
        bitmap=memoryview(zxingcpp.write_barcode(zxingcpp.BarcodeFormat.QRCode,link,240,240))
        image=Image.frombytes('L',(bitmap.shape[1],bitmap.shape[0]),bitmap.tobytes())
        stream=io.BytesIO();image.save(stream,format='PNG')
        data=base64.b64encode(stream.getvalue()).decode()
        if 'evil' in link:
            with pytest.raises(ValueError):
                decode_registration_qr(data)
        else:
            assert decode_registration_qr(data)==link


@pytest.mark.parametrize('preferences,balance,amount,confirmed',[
    ({'automatic_payment':False,'max_amount':20},100,12,12),
    ({'automatic_payment':True,'max_amount':10},100,12,12),
    ({'automatic_payment':True,'max_amount':20},0,12,12),
    ({'automatic_payment':True,'max_amount':20},100,12,11),
])
def test_web_booking_fails_before_reservation_without_consent_funds_or_price_match(tmp_path,monkeypatch,preferences,balance,amount,confirmed):
    from decimal import Decimal
    from types import SimpleNamespace
    from datetime import datetime,timezone
    from padelbot import web_worker,client,booking,checkout
    from padelbot.config import settings
    monkeypatch.setattr(web_worker,'initialise_key',lambda:None)
    monkeypatch.setattr(settings,'padelbot_rules_file',tmp_path/'rules.sqlite3')
    me={'id':123,'balance':balance}
    class Client:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def me(self):return me
    monkeypatch.setattr(client,'BackboneClient',Client)
    target=datetime(2027,1,1,18,tzinfo=timezone.utc)
    monkeypatch.setattr(booking,'find_slots',lambda *a,**kw:[SimpleNamespace(start_dt=target,bookable_product_id=100,bookable_linked_product_id=123)])
    monkeypatch.setattr(checkout,'slot_price',lambda *a:Decimal(amount))
    monkeypatch.setattr(booking,'book',lambda *a,**kw:pytest.fail('Reservation must not be created'))
    monkeypatch.setenv('PADELWEB_PREFERENCES',json.dumps(preferences))
    with pytest.raises(ValueError):
        web_worker.execute('book',{'target':target.isoformat(),'product':100,'court':123,'amount':confirmed})


def test_quote_identifies_court_by_both_parent_and_linked_product(monkeypatch):
    from datetime import datetime,timezone
    from decimal import Decimal
    from types import SimpleNamespace
    from padelbot import web_worker,client,booking,checkout
    monkeypatch.setattr(web_worker,'initialise_key',lambda:None)
    class Client:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def me(self):return {'id':123,'balance':100}
    monkeypatch.setattr(client,'BackboneClient',Client)
    target=datetime(2027,1,1,18,tzinfo=timezone.utc)
    slots=[SimpleNamespace(start_dt=target,start_iso=target.isoformat(),end_iso="2027-01-01T19:00:00+00:00",bookable_product_id=parent,bookable_linked_product_id=123) for parent in (100,200)]
    monkeypatch.setattr(booking,'find_slots',lambda *a,**kw:slots)
    selected=[]
    monkeypatch.setattr(checkout,'slot_price',lambda client,slot,me:selected.append(slot.bookable_product_id) or Decimal(12))
    result=web_worker.execute('quote',{'target':target.isoformat(),'product':200,'court':123})
    assert selected==[200] and result['product']==200


def test_web_scheduler_uses_rolling_168_hours_across_dst_and_keeps_current_window():
    from datetime import datetime,timedelta,timezone
    from zoneinfo import ZoneInfo
    from padelbot.rules_store import Rule
    from padelbot.web_runtime import rule_window
    tz=ZoneInfo('Europe/Brussels')
    rule=Rule(1,0,'18:00',True,None,'20:00')
    now=datetime(2026,10,19,19,0,tzinfo=tz)
    target,end,opens=rule_window(rule,now)
    assert target.date()==now.date() and end.hour==20
    assert target.astimezone(timezone.utc)-opens==timedelta(hours=168)
    now=datetime(2026,10,19,21,0,tzinfo=tz)
    target,end,opens=rule_window(rule,now)
    assert target.isoformat()=='2026-10-26T18:00:00+01:00'
    assert opens.astimezone(tz).isoformat()=='2026-10-19T19:00:00+02:00'


def test_only_readonly_no_slot_outcome_can_be_rescheduled(portal):
    from padelbot.web_runtime import JobRunner
    _,_,store,(_,alice,_)=portal
    runner=JobRunner(store)
    first=store.enqueue(alice['id'],'rule',{},dedup='occurrence')
    runner.finish(first,'done',{'waiting':True})
    second=store.enqueue(alice['id'],'rule',{},dedup='occurrence')
    assert first!=second
    runner.finish(second,'unknown',{'error':'Outcome unknown'})
    assert store.enqueue(alice['id'],'rule',{},dedup='occurrence')==second
    third=store.enqueue(alice['id'],'rule',{},dedup='transient')
    runner.finish(third,'failed',{'error':'HTTP read failed','safe_to_retry':True})
    assert store.enqueue(alice['id'],'rule',{},dedup='transient')!=third


def test_worker_rechecks_live_consent_and_suspension(portal,monkeypatch):
    from padelbot.web_worker import current_preferences
    _,_,store,(owner,alice,_)=portal
    env=tenant_env(store,alice)
    monkeypatch.setenv('PADELWEB_DB',env['PADELWEB_DB'])
    monkeypatch.setenv('PADELWEB_USER_ID',alice['id'])
    store.preferences(alice['id'],{'automatic_payment':True,'max_amount':20})
    assert current_preferences()['automatic_payment'] is True
    store.preferences(alice['id'],{'automatic_payment':False,'max_amount':0})
    assert current_preferences()['automatic_payment'] is False
    store.suspend(owner['id'],alice['id'],True)
    with pytest.raises(ValueError):
        current_preferences()


def test_pin_failure_requires_explicit_manual_check_and_survives_failed_save(tmp_path,monkeypatch):
    from padelbot import web_device
    from padelbot.config import settings
    failure=tmp_path/'failure.json';failure.write_text('{}')
    monkeypatch.setattr(settings,'padelbot_android_failure_file',failure)
    calls=[]
    monkeypatch.setattr(web_device,'save_profile',lambda pin:calls.append(pin))
    with pytest.raises(ValueError):
        web_device.complete_profile('1234')
    assert not calls and failure.exists()
    def failed(pin):raise ValueError('Profile not verified')
    monkeypatch.setattr(web_device,'save_profile',failed)
    with pytest.raises(ValueError):
        web_device.complete_profile('1234',True)
    assert failure.exists()
    monkeypatch.setattr(web_device,'save_profile',lambda pin:calls.append(pin))
    web_device.complete_profile('1234',True)
    assert calls==['1234'] and not failure.exists()


def test_concurrent_profile_initialisation_never_rotates_an_existing_key(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from padelbot import web_worker
    from padelbot.config import settings
    from padelbot.store import TokenStore
    monkeypatch.setattr(settings,'padelbot_key_file',tmp_path/'key')
    monkeypatch.setattr(settings,'padelbot_token_file',tmp_path/'tokens')
    original=TokenStore.init_key
    calls=[]
    def slow_init(store):
        calls.append(True)
        time.sleep(.05)
        original(store)
    monkeypatch.setattr(TokenStore,'init_key',slow_init)
    with ThreadPoolExecutor(max_workers=3) as pool:
        stores=list(pool.map(lambda _:web_worker.initialise_key(),range(3)))
    assert len(calls)==1
    assert all(s.key_file.read_bytes()==stores[0].key_file.read_bytes() for s in stores)


def test_only_owner_can_discover_or_request_admin(portal):
    _,client,store,(owner,alice,bob)=portal
    for user in (alice,bob):
        headers=sign_in(client,store,user)
        profile=client.get('/api/me').json()
        assert profile['owner'] is False and profile['admin'] is False
        assert client.get('/api/admin/users').status_code==403
        assert client.post('/api/admin/access',json={},headers=headers).status_code==403
        assert client.post('/api/admin/users/'+owner['id']+'/toggle',json={},headers=headers).status_code==403


def test_cards_are_encrypted_in_database_and_never_return_full_number(portal):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    app,client,store,(_,alice,bob)=portal
    app.state.cards.key=AESGCM.generate_key(bit_length=256)
    headers=sign_in(client,store,alice)
    data={'holder':'Example Cardholder','number':'4111 1111 1111 1111','month':12,'year':2035}
    result=client.post('/api/payment-card',json=data,headers=headers)
    assert result.status_code==200
    assert {k:result.json()['card'][k] for k in ('brand','last4','month','year')}=={'brand':'Visa','last4':'1111','month':12,'year':2035}
    first_id=result.json()['card']['id']
    assert '4111111111111111' not in result.text and data['holder'] not in result.text
    for path in [store.path,store.path.with_name(store.path.name+'-wal')]:
        if path.exists():
            blob=path.read_bytes()
            assert b'4111111111111111' not in blob and b'Example Cardholder' not in blob
    with store.conn() as c:
        first=c.execute('SELECT ciphertext,nonce,wrapped_key FROM payment_cards').fetchone()
    second_result=client.post('/api/payment-card',json=data,headers=headers)
    assert second_result.status_code==200
    second_id=second_result.json()['card']['id']
    with store.conn() as c:
        second=c.execute('SELECT ciphertext,nonce,wrapped_key FROM payment_cards WHERE id=?',(second_id,)).fetchone()
    assert all(first[i]!=second[i] for i in range(3))
    headers=sign_in(client,store,bob)
    assert client.get('/api/payment-card').json()['card'] is None
    assert client.delete('/api/payment-card',headers=headers).status_code==200
    headers=sign_in(client,store,alice)
    assert client.get('/api/payment-card').json()['card']['last4']=='1111'
    assert client.delete('/api/payment-card/'+first_id,headers=headers).status_code==200
    assert client.delete('/api/payment-card/'+second_id,headers=headers).status_code==200
    assert client.get('/api/payment-card').json()['card'] is None


def test_card_validation_does_not_echo_pan_or_accept_cvc(portal):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    app,client,store,(_,alice,_)=portal
    app.state.cards.key=AESGCM.generate_key(bit_length=256)
    headers=sign_in(client,store,alice)
    data={'holder':'Example','number':'4111111111111111','month':12,'year':2035,'cvc':'987'}
    response=client.post('/api/payment-card',json=data,headers=headers)
    assert response.status_code==422
    assert data['number'] not in response.text and '987' not in response.text
    del data['cvc']
    data['number']='4111111111111112'
    response=client.post('/api/payment-card',json=data,headers=headers)
    assert response.status_code==400 and data['number'] not in response.text
    assert client.get('/api/payment-card').json()['card'] is None
    assert client.post('/api/payment-card',json=data,headers={'Origin':'http://testserver'}).status_code==403


def test_card_ciphertext_cannot_move_to_another_profile_or_use_wrong_key(portal):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    app,client,store,(_,alice,bob)=portal
    app.state.cards.key=AESGCM.generate_key(bit_length=256)
    headers=sign_in(client,store,alice)
    assert client.post('/api/payment-card',json={'holder':'Example','number':'4111111111111111','month':12,'year':2035},headers=headers).status_code==200
    with store.conn() as c:
        c.execute('UPDATE payment_cards SET user_id=? WHERE user_id=?',(bob['id'],alice['id']))
    sign_in(client,store,bob)
    response=client.get('/api/payment-card')
    assert response.status_code==400 and '4111111111111111' not in response.text


def community_booking(booking_id=123,confirmed=True):
    from datetime import datetime,timedelta,timezone
    start=datetime.now(timezone.utc)+timedelta(days=2)
    return {'id':booking_id,'name':'Padel 1','start':start.isoformat(),'end':(start+timedelta(hours=1)).isoformat(),'confirmed':confirmed}


def test_community_only_shares_verified_own_confirmed_reservations(portal):
    app,client,store,(_,alice,bob)=portal
    community=app.state.community
    booking=community_booking()
    headers=sign_in(client,store,alice)
    assert client.post('/api/community/share',json={'booking_id':123,'shared':True},headers=headers).status_code==400
    community.sync(alice['id'],[booking],device_verified=False)
    assert client.post('/api/community/share',json={'booking_id':123,'shared':True},headers=headers).status_code==400
    community.sync(alice['id'],[booking],device_verified=True)
    assert client.get('/api/community').json()['matches']==[]
    assert client.post('/api/community/share',json={'booking_id':123,'shared':True},headers=headers).status_code==200
    headers=sign_in(client,store,bob)
    assert client.post('/api/community/share',json={'booking_id':123,'shared':True},headers=headers).status_code==400
    match=client.get('/api/community').json()['matches'][0]
    assert match['count']==1 and not match['joined']
    assert 'booking_id' not in match and 'email' not in match
    job=client.post('/api/community/'+match['id']+'/join',json={'participant_id':alice['id']},headers=headers).json()['job_id']
    with store.conn() as c:
        payload=json.loads(c.execute('SELECT payload FROM jobs WHERE id=?',(job,)).fetchone()[0])
    assert payload['participant_id']==bob['id']
    community.sync(alice['id'],[],device_verified=True)
    assert client.get('/api/community').json()['matches']==[]
    with pytest.raises(ValueError):community.join_verified(match['id'],bob['id'])


def test_community_capacity_is_atomic_and_rescheduling_revokes_participation(portal):
    from concurrent.futures import ThreadPoolExecutor
    app,_,store,(_,alice,bob)=portal
    community=app.state.community
    booking=community_booking()
    community.sync(alice['id'],[booking],device_verified=True)
    community.share(alice['id'],123,True)
    match=community.list(bob['id'])[0]
    users=[store.login_google({'sub':'player'+str(i),'email':'player'+str(i)+'@example.test','email_verified':True,'name':'Player '+str(i)}) for i in range(7)]
    def join(user):
        try:community.join_verified(match['id'],user['id']);return True
        except ValueError:return False
    with ThreadPoolExecutor(max_workers=7) as pool:
        joined=list(pool.map(join,users))
    assert sum(joined)==3
    assert community.list(alice['id'])[0]['count']==4
    assert community.list(bob['id'])==[]
    player=next(user for user,did_join in zip(users,joined) if did_join)
    community.join_verified(match['id'],player['id'])
    community.leave(match['id'],player['id'])
    assert community.list(bob['id'])[0]['count']==3
    from datetime import timedelta,datetime
    booking['start']=(datetime.fromisoformat(booking['start'])+timedelta(hours=1)).isoformat()
    booking['end']=(datetime.fromisoformat(booking['end'])+timedelta(hours=1)).isoformat()
    community.sync(alice['id'],[booking],device_verified=True)
    assert community.list(bob['id'])==[]
    assert community.own_shared(alice['id'])[123] is False


def test_worker_confirmation_requires_paid_padel_and_registered_device(monkeypatch,tmp_path):
    from types import SimpleNamespace
    from padelbot.web_worker import verified_bookings
    from padelbot import booking,booking_manager
    from padelbot.config import settings
    profile=tmp_path/'profile.json'
    monkeypatch.setattr(settings,'padelbot_android_profile_file',profile)
    common={'id':123,'sale_id':77,'description':'','start_iso':'2035-01-01T18:00:00Z','end_iso':'2035-01-01T19:00:00Z','raw':{'productId':10},'status':0}
    rows=[SimpleNamespace(**common,paid_for=paid) for paid in (True,False,None)]
    sale_client=SimpleNamespace(get=lambda *args,**kwargs:{'id':77,'customerPersonId':42,'statusCode':0})
    monkeypatch.setattr(booking_manager,'list_my_bookings',lambda *args,**kwargs:rows)
    monkeypatch.setattr(booking,'get_product_names',lambda *args:{10:'Padel 1'})
    assert all(not b['confirmed'] for b in verified_bookings(sale_client,{'id':42})['bookings'])
    profile.write_text('{}')
    assert [b['confirmed'] for b in verified_bookings(sale_client,{'id':42})['bookings']]==[True,False,False]
    for sale in [{'id':77,'customerPersonId':42,'statusCode':1},
                 {'id':77,'customerPersonId':999,'statusCode':0},
                 {'id':78,'customerPersonId':42,'statusCode':0},
                 {'id':77,'customerPersonId':42,'statusCode':False}]:
        provisional=SimpleNamespace(get=lambda *a,**k:sale)
        assert not any(b['confirmed'] for b in verified_bookings(provisional,{'id':42})['bookings'])
    monkeypatch.setattr(booking,'get_product_names',lambda *args:{10:'Tennis 1'})
    assert all(not b['confirmed'] for b in verified_bookings(sale_client,{'id':42})['bookings'])


def test_join_worker_rechecks_organisers_client_and_cancellation_blocks_join(portal,monkeypatch):
    from padelbot import web_worker,client as client_module
    app,_,store,(_,alice,bob)=portal
    community=app.state.community
    booking=community_booking()
    community.sync(alice['id'],[booking],device_verified=True)
    community.share(alice['id'],123,True)
    match=community.list(bob['id'])[0]
    class Client:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def me(self):return {'id':42}
    monkeypatch.setattr(client_module,'BackboneClient',Client)
    monkeypatch.setattr(web_worker,'initialise_key',lambda:None)
    monkeypatch.setenv('PADELWEB_DB',str(store.path))
    monkeypatch.setenv('PADELWEB_USER_ID',alice['id'])
    checks=[]
    def actual(client,me):
        checks.append(me['id'])
        return {'device_verified':True,'bookings':[booking]}
    monkeypatch.setattr(web_worker,'verified_bookings',actual)
    payload={'match_id':match['id'],'participant_id':bob['id']}
    assert web_worker.execute('community_join',payload)['joined'] is True
    assert checks==[42]
    assert community.list(bob['id'])[0]['count']==2
    booking['confirmed']=False
    with pytest.raises(ValueError):web_worker.execute('community_join',payload)
    assert checks==[42,42] and community.list(bob['id'])==[]


def test_card_vault_never_regenerates_a_missing_existing_key(portal,tmp_path):
    import os
    from padelbot.web_cards import CardVault
    _,_,store,(_,alice,_)=portal
    key=tmp_path/'separate-private.key'
    key.write_bytes(os.urandom(32))
    vault=CardVault(store,key)
    vault.save(alice['id'],holder='Example',number='4111111111111111',month=12,year=2035)
    key.unlink()
    with pytest.raises(FileNotFoundError):CardVault(store,key)
    assert not key.exists()
    with pytest.raises(ValueError):CardVault(store,None).summary(alice['id'])


def test_community_names_do_not_expose_email_or_crash_on_blank_name():
    from padelbot.web_community import public_name
    assert public_name('Alice Example')=='Alice'
    assert public_name('alice@example.test')=='Speler'
    assert public_name('   ')=='Speler'
