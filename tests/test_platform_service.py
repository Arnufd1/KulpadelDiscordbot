import asyncio,json,time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit,parse_qs

import pytest
from test_web import portal,sign_in
from padelbot.platform_service import PlatformService,install_url,validate_payment


def heartbeat(service):
    with service.store.conn() as c:c.execute("INSERT OR REPLACE INTO platform_runtime VALUES('discord_heartbeat',?)",(str(time.time()),))


def test_install_requests_minimal_permissions_for_correct_context():
    params=parse_qs(urlsplit(install_url('123456789')).query)
    assert params['scope']==['bot applications.commands'] and params['integration_type']==['0']
    assert int(params['permissions'][0])&8==0
    assert parse_qs(urlsplit(install_url('123456789',personal=True)).query)['scope']==['applications.commands']
    with pytest.raises(ValueError):install_url('https://evil.test')


def test_linked_discord_and_web_use_one_identity_and_one_booking_job(portal):
    app,client,store,(_,alice,bob)=portal
    service=app.state.platform
    service.hub.register(alice);service.hub.link(alice['id'],12345)
    assert service.discord_user(12345)['id']==alice['id']
    with pytest.raises(ValueError):service.discord_user(54321)
    store.preferences(alice['id'],{'payment_mode':'discord','automatic_payment':False,'max_amount':0})
    heartbeat(service)
    headers=sign_in(client,store,alice)
    payload={'product':100,'court':200,'target':'2030-01-01T18:00:00Z','amount':12}
    first=client.post('/api/book',json=payload,headers=headers).json()['job_id']
    second=service.enqueue(service.discord_user(12345),'book',{**payload,'target':'2030-01-01T19:00:00+01:00'})
    assert first==second
    sign_in(client,store,bob)
    assert client.get('/api/jobs/'+first).status_code==404
    store.suspend('owner',alice['id'],True)
    with pytest.raises(ValueError):service.discord_user(12345)


def test_website_payment_link_booking_does_not_require_discord(portal):
    app,_,store,(_,alice,_)=portal;service=app.state.platform
    store.preferences(alice['id'],{'payment_mode':'discord'})
    payload={'product':100,'court':200,'target':'2030-01-01T18:00:00Z','amount':12}
    assert service.enqueue(alice,'book',payload)
    assert client_jobs(store)==1


def client_jobs(store):
    with store.conn() as c:return c.execute('SELECT count(*) FROM jobs').fetchone()[0]


def test_oauth_state_is_session_user_bound_expires_and_consumes_once(portal):
    app,_,_,(_,alice,bob)=portal;s=app.state.platform
    state=s.oauth_start(alice['id'],'session-a')
    assert not s.oauth_consume(state,'session-b',alice['id'])
    assert not s.oauth_consume(state,'session-a',bob['id'])
    assert s.oauth_consume(state,'session-a',alice['id'])
    assert not s.oauth_consume(state,'session-a',alice['id'])


def test_local_card_or_covering_balance_is_required_for_automatic_payment():
    validate_payment({'balance':12},Decimal('12'),'kul')
    validate_payment({'balance':0},Decimal('12'),'discord')
    with pytest.raises(ValueError,match='betaalkaart'):validate_payment({'balance':0},Decimal('12'),'kul')
    validate_payment({'balance':0},Decimal('12'),'kul',local_card={'network':'mastercard','expired':False})
    with pytest.raises(ValueError):validate_payment({'balance':0},Decimal('12'),'kul',local_card={'network':'visa','expired':True})


def test_private_delivery_is_encrypted_tenant_bound_and_never_repeated(portal):
    app,client,store,(_,alice,bob)=portal;s=app.state.platform;s.hub.register(alice);s.hub.link(alice['id'],12345)
    url='https://payment.direct.worldline-solutions.com/checkout/private-example'
    s.queue_payment(alice['id'],100,url,12,'2030-01-01T18:00:00Z')
    s.queue_payment(alice['id'],100,url,12,'2030-01-01T18:00:00Z')
    with store.conn() as c:
        rows=c.execute('SELECT * FROM discord_deliveries').fetchall()
        assert len(rows)==1 and url not in rows[0]['payload']
    assert s.delivery_status(bob['id'])==[]
    calls=[]
    class Recipient:
        async def send(self,message,**kwargs):calls.append((message,kwargs['view'].children[0].url))
    class Bot:
        def get_user(self,ident):assert ident==12345;return Recipient()
    from padelbot.discord_delivery import deliver_once
    asyncio.run(deliver_once(Bot(),s));asyncio.run(deliver_once(Bot(),s))
    assert len(calls)==1 and calls[0][1]==url
    with store.conn() as c:
        row=c.execute('SELECT status,payload FROM discord_deliveries').fetchone()
        assert tuple(row)==('sent','')
    headers=sign_in(client,store,alice)
    me=client.get('/api/me').json()
    assert url not in json.dumps(me)


def test_delivery_cannot_leak_to_changed_or_suspended_identity(portal):
    app,_,store,(_,alice,_)=portal;s=app.state.platform;s.hub.register(alice);s.hub.link(alice['id'],12345)
    s.queue_payment(alice['id'],100,'https://payment.direct.worldline-solutions.com/example',12,'2030-01-01T18:00:00Z')
    store.suspend('owner',alice['id'],True)
    class Bot:
        def get_user(self,_):pytest.fail('Suspended profile must not receive a link')
    from padelbot.discord_delivery import deliver_once
    asyncio.run(deliver_once(Bot(),s))
    assert s.delivery_status(alice['id'])[0]['status']=='cancelled'
    with store.conn() as c:
        assert c.execute('SELECT payload FROM discord_deliveries WHERE user_id=?',(alice['id'],)).fetchone()[0]==''


def test_discord_oauth_links_verified_identity_without_storing_token(portal,monkeypatch):
    app,client,store,(_,alice,_)=portal
    app.state.config.discord_client_id='123456789';app.state.config.discord_client_secret='test-client-secret'
    assert client.get('/auth/discord',follow_redirects=False).status_code==401
    sign_in(client,store,alice)
    response=client.get('/auth/discord',follow_redirects=False)
    query=parse_qs(urlsplit(response.headers['location']).query)
    assert query['scope']==['identify bot applications.commands']
    assert query['redirect_uri']==['http://testserver/auth/discord/callback']
    class Response:
        def __init__(self,data):self.data=data
        def json(self):return self.data
        def raise_for_status(self):pass
    class OAuth:
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        async def post(self,url,data):return Response({'access_token':'ephemeral-private-token'})
        async def get(self,url,headers):
            assert headers=={'Authorization':'Bearer ephemeral-private-token'}
            return Response({'id':'12345','username':'Player'})
    monkeypatch.setattr('padelbot.web.httpx.AsyncClient',lambda **kw:OAuth())
    callback='/auth/discord/callback?code=example&state='+query['state'][0]
    response=client.get(callback,follow_redirects=False)
    assert response.status_code==303 and app.state.platform.discord_user(12345)['id']==alice['id']
    assert client.get(callback,follow_redirects=False).status_code==400
    assert 'ephemeral-private-token' not in store.path.read_bytes().decode('latin1')


def test_discord_mode_creates_one_hosted_checkout_without_touching_saved_cards(monkeypatch,tmp_path):
    from padelbot import booking,payment,worldline_driver
    from padelbot.config import settings
    monkeypatch.setattr(settings,'padelbot_rules_file',tmp_path/'rules.sqlite3')
    monkeypatch.setattr(booking,'add_to_cart',lambda *a,**kw:None)
    monkeypatch.setattr(booking,'create_sale',lambda *a,**kw:{'id':12,'total':12})
    monkeypatch.setattr(booking,'fetch_booking_id',lambda *a:55)
    calls=[]
    def checkout(client,**kwargs):
        calls.append(kwargs)
        return {'userInitiatedPaymentResult':{'bSuccess':True,'hostedPaymentPageUrl':'https://payment.direct.worldline-solutions.com/example'}}
    monkeypatch.setattr(payment,'create_payment',checkout)
    monkeypatch.setattr(payment,'auto_pay',lambda *a,**kw:pytest.fail('No automatic debit in Discord-link mode'))
    monkeypatch.setattr(worldline_driver,'load_card',lambda:pytest.fail('No card loaded in Discord-link mode'))
    slot=booking.Slot(100,200,'2030-01-01T18:00:00Z','2030-01-01T19:00:00Z',12,{})
    result=booking.book(object(),slot,member_id=42,me={'id':42,'balance':0},expected_amount=Decimal(12),payment_mode='discord')
    assert not result.paid and result.payment_url and len(calls)==1
    assert calls[0]['method']==payment.PayMethod.CREDIT_CARD


def test_kul_mode_fails_before_creating_sale_if_no_authorised_method(monkeypatch,tmp_path):
    from padelbot import booking
    from padelbot.config import settings
    monkeypatch.setattr(settings,'padelbot_rules_file',tmp_path/'rules.sqlite3')
    monkeypatch.setattr(booking,'add_to_cart',lambda *a,**kw:pytest.fail('No cart write without an authorised method'))
    slot=booking.Slot(100,200,'2030-01-01T18:00:00Z','2030-01-01T19:00:00Z',12,{})
    with pytest.raises(ValueError):booking.book(object(),slot,member_id=42,me={'id':42,'balance':0},expected_amount=Decimal(12),payment_mode='kul')


def test_discord_confirmation_writes_once_and_rejects_other_user(portal,monkeypatch):
    from padelbot import discord_platform
    from test_discord_ui import interaction
    app,_,_,(_,alice,_)=portal;service=app.state.platform
    service.hub.register(alice);service.hub.link(alice['id'],12345)
    async def run():
        writes=[]
        async def task(service,user,kind,payload):
            writes.append(user['id']);await asyncio.sleep(.01);return {'message':'Betaallink volgt via Discord.'}
        monkeypatch.setattr(discord_platform,'job',task)
        view=discord_platform.ConfirmView(service,alice,12345,{'start':'2030-01-01T18:00:00Z','product':100,'court':200},{'amount':'12'})
        assert not await view.interaction_check(interaction(user=55555))
        assert not writes
        request=interaction(user=12345)
        await asyncio.gather(view.confirm(request),view.confirm(request))
        assert writes==[alice['id']]
    asyncio.run(run())


def test_preflight_and_checkout_edit_the_same_private_message(portal):
    app,_,store,(_,alice,_)=portal;s=app.state.platform;s.hub.register(alice);s.hub.link(alice['id'],12345)
    sent=[];edits=[]
    message=SimpleNamespace(id=9,channel=SimpleNamespace(id=8))
    class Recipient:
        async def send(self,content,**kwargs):sent.append(content);return message
    class Partial:
        async def edit(self,**kwargs):edits.append(kwargs);return message
    class Channel:
        def get_partial_message(self,ident):assert ident==9;return Partial()
    class Bot:
        def get_user(self,ident):assert ident==12345;return Recipient()
        def get_channel(self,ident):assert ident==8;return Channel()
    from padelbot.discord_delivery import deliver_once
    s._queue_notice(alice['id'],'prepare:'+alice['id']+':example',{'kind':'prepare','amount':'12','target':'2030-01-01T18:00:00Z'})
    asyncio.run(deliver_once(Bot(),s))
    prepared=s.prepare_delivery(alice['id'],'example',12,'2030-01-01T18:00:00Z')
    s.queue_payment(alice['id'],12,'https://payment.direct.worldline-solutions.com/example',12,'2030-01-01T18:00:00Z',prepared)
    asyncio.run(deliver_once(Bot(),s))
    assert len(sent)==1 and len(edits)==1
    assert edits[0]['view'].children[0].url=='https://payment.direct.worldline-solutions.com/example'
    with pytest.raises(ValueError,match='niets geboekt'):
        s.prepare_delivery(alice['id'],'timed-out',12,'2030-01-01T18:00:00Z',timeout=0)
    asyncio.run(deliver_once(Bot(),s))
    assert len(sent)==1


@pytest.mark.parametrize('mode',['discord','kul'])
@pytest.mark.parametrize('can_dm',[False,True])
@pytest.mark.parametrize('payment_pending',[False,True])
def test_shared_worker_keeps_private_website_checkout_when_discord_unavailable(portal,monkeypatch,can_dm,mode,payment_pending):
    app,_,store,(_,alice,_)=portal;s=app.state.platform
    s.hub.register(alice)
    from padelbot.web_cards import CardVault
    key=store.root/'test-vault.key';key.write_bytes(b'k'*32)
    store.card_vault_key_file=key
    s.cards=CardVault(store,key)
    s.cards.save(alice['id'],holder='Alice',number='4242424242424242',month=12,year=2035)
    if can_dm:s.hub.link(alice['id'],12345)
    store.preferences(alice['id'],{'automatic_payment':False,'payment_mode':mode,'max_amount':0})
    from padelbot import web_worker,client,booking,booking_manager,checkout
    from padelbot.config import settings
    monkeypatch.setenv('PADELWEB_DB',str(store.path));monkeypatch.setenv('PADELWEB_USER_ID',alice['id']);monkeypatch.setenv('PADELWEB_JOB_ID','example-job')
    monkeypatch.setattr(web_worker,'initialise_key',lambda:None);monkeypatch.setattr(web_worker,'platform_context',lambda:s.community)
    monkeypatch.setattr(settings,'padelbot_rules_file',store.root/'isolated-rules.sqlite3')
    class Client:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def me(self):return {'id':42,'balance':0}
        def get(self,path,params):pytest.fail('No KUL stored-method lookup')
    monkeypatch.setattr(client,'BackboneClient',Client)
    slot=booking.Slot(100,200,'2030-01-01T18:00:00Z','2030-01-01T19:30:00Z',12,{})
    monkeypatch.setattr(booking,'find_slots',lambda *a,**kw:[slot]);monkeypatch.setattr(checkout,'slot_price',lambda *a:Decimal(12))
    monkeypatch.setattr(booking_manager,'list_my_bookings',lambda *a,**kw:[])
    def prepare(*a,**kw):
        pytest.fail('Website checkout must not depend on a Discord preflight')
    monkeypatch.setattr(PlatformService,'prepare_delivery',prepare)
    writes=[]
    def reserve(*a,**kw):
        writes.append(kw)
        if mode=='kul':
            card=kw['payment_card_loader']()
            assert card.holder=='Alice' and card.number=='4242424242424242' and card.cvv==''
        else:assert kw['payment_card_loader'] is None
        return SimpleNamespace(paid=False,sale_id=12,booking_id=55,payment_url='https://payment.direct.worldline-solutions.com/example',payment_pending=payment_pending)
    monkeypatch.setattr(booking,'book',reserve)
    payload={'product':100,'court':200,'target':slot.start_iso,'amount':12}
    result=web_worker.execute('book',payload)
    assert len(writes)==1 and writes[0]['payment_mode']==mode
    assert result['payment_via_discord']==(can_dm and not payment_pending)
    assert result['payment_available'] is not payment_pending and result['payment_pending'] is payment_pending and not result['paid']
    assert 'worldline' not in json.dumps(result)
    if payment_pending:
        assert not s.private_payments(alice['id'])
        assert 'voordat je opnieuw betaalt' in result['message']
    else:assert s.private_payments(alice['id'])[0]['url']=='https://payment.direct.worldline-solutions.com/example'


def test_tokenized_kul_methods_are_owner_bound_current_and_explicitly_preferred():
    from padelbot.payment import attach_stored_method
    good={'id':10,'personId':42,'type':3,'paymentProviderType':8,'preferred':True,'expirationDate':'2035-12-01'}
    class Client:
        def get(self,path,params):
            assert path=='/stored-pay-methods' and params['s']=={'personId':42}
            return {'data':[{**good,'id':99,'personId':43},good,{**good,'id':11,'expirationDate':'2020-01-01'},
                {**good,'id':12,'paymentProviderType':999},{**good,'id':13,'expirationDate':'invalid'}]}
    me=attach_stored_method(Client(),{'id':42,'balance':0})
    assert me['_stored_method']=={'id':10,'type':3,'paymentProviderType':8,'preferred':True}
    with pytest.raises(ValueError):validate_payment(me,Decimal(12),'kul')
    assert 'personId' not in me['_stored_method']


def test_stored_method_payment_rechecks_ownership_before_posting():
    from padelbot.payment import create_payment,PayMethod
    writes=[]
    class Client:
        def get(self,path,params):return {'data':[{'id':10,'personId':43,'type':3,'paymentProviderType':8}]}
        def post(self,*a,**kw):writes.append(a);pytest.fail('Other account payment method must not be used')
    with pytest.raises(RuntimeError):create_payment(Client(),sale_id=12,member_id=42,amount=12,method=PayMethod.CREDIT_CARD,stored_method_id=10)
    assert not writes


@pytest.mark.parametrize('confirmed',[False,True])
def test_local_card_requires_backend_confirmation_and_never_uses_kul_token(monkeypatch,tmp_path,confirmed):
    from padelbot import booking,payment,worldline_driver
    from padelbot.config import settings
    monkeypatch.setattr(settings,'padelbot_rules_file',tmp_path/'rules.sqlite3')
    monkeypatch.setattr(booking,'add_to_cart',lambda *a,**kw:None)
    monkeypatch.setattr(booking,'create_sale',lambda *a,**kw:{'id':12,'total':12})
    monkeypatch.setattr(booking,'fetch_booking_id',lambda *a:55)
    calls=[]
    def pay(*a,**kw):calls.append(kw);return {'userInitiatedPaymentResult':{'bSuccess':True,'hostedPaymentPageUrl':'https://payment.direct.worldline-solutions.com/example'}}
    monkeypatch.setattr(payment,'create_payment',pay)
    monkeypatch.setattr(payment,'wait_for_payment_confirmation',lambda *a,**kw:confirmed)
    monkeypatch.setattr(worldline_driver,'load_card',lambda:pytest.fail('No legacy card store'))
    from padelbot.card_store import Card
    card=Card('Alice','4242424242424242',12,2035,'')
    driver=[]
    def checkout(**kwargs):driver.append(kwargs);return worldline_driver.WorldlineResult(success=True,submitted=True)
    monkeypatch.setattr(worldline_driver,'pay_via_worldline',checkout)
    slot=booking.Slot(100,200,'2030-01-01T18:00:00Z','2030-01-01T19:30:00Z',12,{})
    result=booking.book(object(),slot,member_id=42,me={'id':42,'balance':0,'_stored_method':{'id':10,'type':3}},expected_amount=Decimal(12),payment_mode='kul',payment_card=card)
    assert result.paid is confirmed and result.payment_pending is not confirmed
    assert ('stored_method_id' not in calls[0] or calls[0]['stored_method_id'] is None)
    assert driver[0]['card'] is card and driver[0]['allow_missing_cvv'] is True
    assert not result.payment_url


def test_private_website_checkout_survives_discord_delivery_and_is_tenant_bound(portal):
    app,_,store,(_,alice,bob)=portal;s=app.state.platform
    url='https://payment.direct.worldline-solutions.com/checkout/private-token'
    assert s.queue_payment(alice['id'],100,url,12,'2030-01-01T18:00:00Z',booking_id=55) is False
    payment=s.private_payments(alice['id'])[0]
    assert payment['url']==url and payment['booking_id']==55 and payment['status']=='pending'
    assert s.private_payments(bob['id'])==[]
    with store.conn() as c:
        row=c.execute('SELECT payload FROM private_payments WHERE user_id=?',(alice['id'],)).fetchone()
        assert url not in row[0]
        c.execute('INSERT INTO private_payments(user_id,sale_id,booking_id,payload,created) VALUES(?,?,?,?,?)',(bob['id'],100,55,row[0],time.time()))
    assert s.private_payments(bob['id'])==[]
    assert url not in store.path.read_bytes().decode('latin1')


def test_confirmed_booking_and_fresh_reminder_are_sent_once_and_clear_payment(portal):
    from datetime import datetime,timezone
    from padelbot.discord_delivery import deliver_once
    app,_,store,(_,alice,_)=portal;s=app.state.platform;s.hub.register(alice);s.hub.link(alice['id'],12345)
    now=time.time();target=datetime.fromtimestamp(now+1790,timezone.utc).isoformat()
    s.queue_payment(alice['id'],100,'https://payment.direct.worldline-solutions.com/private',12,target,booking_id=55)
    s.queue_confirmed_booking(alice['id'],55,target,12)
    s.queue_confirmed_booking(alice['id'],55,target,12)
    assert s.private_payments(alice['id'])==[]
    s.queue_due_reminders();s.queue_due_reminders()
    messages=[]
    class Recipient:
        async def send(self,content,**kwargs):messages.append(content);return SimpleNamespace(id=9,channel=SimpleNamespace(id=8))
    class Bot:
        def get_user(self,ident):assert ident==12345;return Recipient()
    for _ in range(4):asyncio.run(deliver_once(Bot(),s))
    assert len(messages)==2
    assert messages[0].startswith('Je hebt geboekt.')
    assert messages[1].startswith('Padel begint over 30 minuten.')


def test_reminders_require_fresh_confirmed_kul_snapshot_and_ignore_cancelled(portal):
    from datetime import datetime,timezone
    from padelbot.discord_delivery import deliver_once
    app,_,store,(_,alice,_)=portal;s=app.state.platform;s.hub.register(alice);s.hub.link(alice['id'],12345)
    now=time.time();target=datetime.fromtimestamp(now+1790,timezone.utc).isoformat()
    booking={'id':55,'start':target,'confirmed':True}
    s.sync_verified_bookings(alice['id'],[booking],device_verified=True)
    assert s.delivery_status(alice['id'])==[]  # Historical lookup is not a new booking.
    with store.conn() as c:c.execute('UPDATE booking_notifications SET verified=?',(now-301,))
    s.queue_due_reminders()
    assert s.delivery_status(alice['id'])==[]
    assert s.users_needing_booking_refresh()==[alice['id']]
    s.sync_verified_bookings(alice['id'],[booking],device_verified=True)
    s.queue_due_reminders()
    s.sync_verified_bookings(alice['id'],[],device_verified=True)
    class Bot:
        def get_user(self,_):pytest.fail('Cancelled reservation must not send a reminder')
    asyncio.run(deliver_once(Bot(),s))
    assert s.delivery_status(alice['id'])[0]['status']=='cancelled'


def test_pending_payment_confirmation_from_kul_read_not_paidfor_alone(portal):
    app,_,store,(_,alice,_)=portal;s=app.state.platform;s.hub.register(alice);s.hub.link(alice['id'],12345)
    target='2030-01-01T18:00:00Z'
    s.queue_payment(alice['id'],100,'https://payment.direct.worldline-solutions.com/private',12,target,booking_id=55)
    s.sync_verified_bookings(alice['id'],[{'id':55,'start':target,'paid':True,'confirmed':False}],device_verified=True)
    assert len(s.private_payments(alice['id']))==1
    s.sync_verified_bookings(alice['id'],[{'id':55,'start':target,'paid':True,'confirmed':True}],device_verified=False)
    assert len(s.private_payments(alice['id']))==1
    s.sync_verified_bookings(alice['id'],[{'id':55,'start':target,'paid':True,'confirmed':True}],device_verified=True)
    assert not s.private_payments(alice['id'])
    with store.conn() as c:
        assert c.execute("SELECT count(*) FROM discord_deliveries WHERE id LIKE 'booked:%'").fetchone()[0]==1


def test_kul_sale_confirmation_finds_checkout_when_initial_booking_id_was_delayed(portal):
    app,_,store,(_,alice,_)=portal;s=app.state.platform;s.hub.register(alice);s.hub.link(alice['id'],12345)
    target='2030-01-01T18:00:00Z'
    s.queue_payment(alice['id'],100,'https://payment.direct.worldline-solutions.com/private',12,target)
    s.sync_verified_bookings(alice['id'],[{'id':55,'sale_id':999,'start':target,'confirmed':True}],device_verified=True)
    assert len(s.private_payments(alice['id']))==1
    s.sync_verified_bookings(alice['id'],[{'id':55,'sale_id':100,'start':target,'confirmed':True}],device_verified=True)
    assert not s.private_payments(alice['id'])
    with store.conn() as c:
        assert c.execute('SELECT booking_id,status FROM private_payments WHERE user_id=?',(alice['id'],)).fetchone()['booking_id']==55


@pytest.mark.parametrize('mode',['kul','discord'])
def test_modern_checkout_uses_covering_balance_before_credit_card(monkeypatch,tmp_path,mode):
    from padelbot import booking,payment,worldline_driver
    from padelbot.config import settings
    monkeypatch.setattr(settings,'padelbot_rules_file',tmp_path/'rules.sqlite3')
    monkeypatch.setattr(booking,'add_to_cart',lambda *a,**kw:None)
    monkeypatch.setattr(booking,'create_sale',lambda *a,**kw:{'id':12,'total':12})
    monkeypatch.setattr(booking,'fetch_booking_id',lambda *a:55)
    calls=[]
    monkeypatch.setattr(payment,'create_payment',lambda *a,**kw:calls.append(kw) or {})
    monkeypatch.setattr(payment,'wait_for_payment_confirmation',lambda *a,**kw:True)
    monkeypatch.setattr(worldline_driver,'load_card',lambda:pytest.fail('No legacy card vault'))
    slot=booking.Slot(100,200,'2030-01-01T18:00:00Z','2030-01-01T19:30:00Z',12,{})
    result=booking.book(object(),slot,member_id=42,me={'id':42,'balance':12,'directDebitMandate':True,'directDebitSpec':42},expected_amount=Decimal(12),payment_mode=mode)
    assert result.paid and not result.payment_url and len(calls)==1
    assert calls[0]['method']==payment.PayMethod.ACCOUNT


def test_modern_card_mode_never_substitutes_direct_debit():
    with pytest.raises(ValueError):validate_payment({'balance':0,'directDebitMandate':True,'directDebitSpec':42},Decimal(12),'kul')
    with pytest.raises(ValueError):validate_payment({'balance':0,'_stored_method':{'id':10,'type':2}},Decimal(12),'kul')


def test_upcoming_existing_booking_gets_one_fresh_check_then_stops_polling(portal):
    from datetime import datetime,timezone
    app,_,store,(_,alice,_)=portal;s=app.state.platform;s.hub.register(alice);s.hub.link(alice['id'],12345)
    now=time.time();target=datetime.fromtimestamp(now+1790,timezone.utc).isoformat()
    end=datetime.fromtimestamp(now+7190,timezone.utc).isoformat()
    s.community.sync(alice['id'],[{'id':55,'start':target,'end':end,'confirmed':True}],device_verified=True)
    assert s.users_needing_booking_refresh()==[alice['id']]
    s.sync_verified_bookings(alice['id'],[{'id':55,'start':target,'confirmed':True}],device_verified=True)
    s.queue_due_reminders()
    assert s.users_needing_booking_refresh(now=now+121)==[]


def test_reminder_checks_exact_booking_even_when_another_same_time_is_cancelled(portal):
    from datetime import datetime,timezone
    from padelbot.discord_delivery import deliver_once
    app,_,store,(_,alice,_)=portal;s=app.state.platform;s.hub.register(alice);s.hub.link(alice['id'],12345)
    now=time.time();target=datetime.fromtimestamp(now+1790,timezone.utc).isoformat()
    s.sync_verified_bookings(alice['id'],[{'id':54,'start':target,'confirmed':True},{'id':55,'start':target,'confirmed':True}],device_verified=True)
    s.sync_verified_bookings(alice['id'],[{'id':55,'start':target,'confirmed':True}],device_verified=True)
    s.queue_due_reminders()
    messages=[]
    class Recipient:
        async def send(self,content,**kwargs):messages.append(content);return SimpleNamespace(id=9,channel=SimpleNamespace(id=8))
    class Bot:
        def get_user(self,_):return Recipient()
    asyncio.run(deliver_once(Bot(),s))
    assert len(messages)==1 and messages[0].startswith('Padel begint over 30 minuten.')


def test_notification_failure_cannot_turn_successful_payment_into_retry(portal,monkeypatch):
    from padelbot.web_runtime import JobRunner
    app,_,store,(_,alice,_)=portal;s=app.state.platform
    payload={'product':100,'court':200,'target':'2030-01-01T18:00:00Z','amount':12}
    ident=s.enqueue(alice,'book',payload)
    def unavailable(*args,**kwargs):raise OSError('Simulated notification storage outage')
    monkeypatch.setattr(PlatformService,'queue_confirmed_booking',unavailable)
    JobRunner(store).finish(ident,'done',{'paid':True,'booking_id':55,'sale_id':12,'amount':'12'})
    outcome=store.job(alice['id'],ident)
    assert outcome['status']=='done' and outcome['result']['paid'] is True
    assert s.enqueue(alice,'book',payload)==ident


@pytest.mark.parametrize('state',['paid','expired','started'])
def test_delayed_discord_checkout_does_not_send_unusable_payment_link(portal,state):
    from datetime import datetime,timezone
    from padelbot.discord_delivery import deliver_once
    app,_,store,(_,alice,_)=portal;s=app.state.platform
    s.hub.register(alice);s.hub.link(alice['id'],12345)
    target=datetime.fromtimestamp(time.time()+(-60 if state=='started' else 3600),timezone.utc).isoformat()
    s.queue_payment(alice['id'],100,'https://payment.direct.worldline-solutions.com/private',12,target,booking_id=55)
    with store.conn() as c:
        if state=='paid':c.execute("UPDATE private_payments SET status='paid'")
        if state=='expired':c.execute('UPDATE private_payments SET created=?',(time.time()-3601,))
    class Bot:
        def get_user(self,_):pytest.fail('An unusable payment link must not be sent')
    asyncio.run(deliver_once(Bot(),s))
    assert s.delivery_status(alice['id'])[0]['status']=='cancelled'
