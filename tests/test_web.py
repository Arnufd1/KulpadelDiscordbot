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
    body={'target':'2026-10-19T18:00:00Z','court':123,'amount':12}
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
    assert client.post('/api/device/control',json={'action':'screen'},headers=headers).status_code==400
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
    monkeypatch.setattr(booking,'find_slots',lambda *a,**kw:[SimpleNamespace(start_dt=target,bookable_linked_product_id=123)])
    monkeypatch.setattr(checkout,'slot_price',lambda *a:Decimal(amount))
    monkeypatch.setattr(booking,'book',lambda *a,**kw:pytest.fail('Reservation must not be created'))
    monkeypatch.setenv('PADELWEB_PREFERENCES',json.dumps(preferences))
    with pytest.raises(ValueError):
        web_worker.execute('book',{'target':target.isoformat(),'court':123,'amount':confirmed})


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
