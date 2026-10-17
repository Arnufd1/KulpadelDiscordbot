"""Local-vault checkout boundaries; all provider operations are synthetic."""
import json
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal

import pytest

from test_web import portal
from padelbot.card_store import Card, validate_card
from padelbot.platform_service import PlatformService, validate_autobook_payment
from padelbot.web_cards import CardVault
from padelbot.web_runtime import tenant_env


PAN='4242424242424242'
CARD=Card('Alice',PAN,12,2035,'')


def configure_vault(store,service):
    key=store.root/'card-test.key';key.write_bytes(b'v'*32)
    store.card_vault_key_file=key
    service.cards=CardVault(store,key)
    return service.cards,key


def test_local_primary_updates_autobook_immediately_without_a_kul_token(portal):
    app,_,store,(_,alice,bob)=portal;service=app.state.platform
    vault,key=configure_vault(store,service)
    service.remember_payment(alice['id'],{'balance':8,'direct_debit':False,'stored_card':True})
    assert not service.payment_status(alice['id'])['stored_card']
    assert not service.can_autobook(alice['id'])
    first=vault.save(alice['id'],holder='Alice',number=PAN,month=12,year=2035,funding='debit')
    service.save_preferences(alice,{'automatic_payment':True,'payment_mode':'kul','max_amount':15})
    assert service.can_autobook(alice['id'])
    payment=service.payment_status(alice['id'])
    assert payment['balance']=='8' and payment['local_card']['funding']=='debit'
    assert payment['stored_card'] and not payment['stored_method']
    assert not service.payment_status(bob['id'])
    # Another process/restart reads the same encrypted row with the same key.
    second=PlatformService(store,service.community,cards=CardVault(store,key))
    assert second.can_autobook(alice['id'])
    vault.delete(alice['id'],first['id'])
    assert not second.can_autobook(alice['id'])
    assert not second.payment_status(alice['id'])['stored_card']


def test_card_alone_does_not_invent_a_checked_kul_balance(portal):
    app,_,store,(_,alice,_)=portal;service=app.state.platform
    vault,_=configure_vault(store,service)
    vault.save(alice['id'],holder='Alice',number=PAN,month=12,year=2035)
    status=service.payment_status(alice['id'])
    assert status['local_card']['last4']=='4242'
    assert status['balance'] is None and status['checked'] is None


@pytest.mark.parametrize('card',[None,{'network':'visa','expired':True},{'network':'amex','expired':False}])
def test_autobook_needs_supported_current_local_card_even_with_balance(card):
    with pytest.raises(ValueError):validate_autobook_payment({'balance':999,'_stored_method':{'type':3}},'kul',local_card=card)


def test_cvc_optional_only_for_explicit_vault_driver_and_repr_is_redacted():
    validate_card(CARD,allow_missing_cvv=True)
    with pytest.raises(ValueError):validate_card(CARD)
    with pytest.raises(ValueError):validate_card(replace(CARD,cvv='12'),allow_missing_cvv=True)
    assert PAN not in repr(CARD) and '923' not in repr(replace(CARD,cvv='923'))


def test_worker_summary_and_child_environment_never_include_card_material(portal,monkeypatch):
    from padelbot import web_worker
    app,_,store,(_,alice,bob)=portal;service=app.state.platform
    vault,key=configure_vault(store,service)
    vault.save(alice['id'],holder='Alice',number=PAN,month=12,year=2035)
    vault.save(bob['id'],holder='Bob',number='5555555555554444',month=12,year=2035)
    monkeypatch.setenv('PADELWEB_DB',str(store.path))
    monkeypatch.setenv('PADELWEB_USER_ID',alice['id'])
    monkeypatch.setenv('PADELWEB_CARD_VAULT_KEY_FILE',str(key))
    monkeypatch.setattr(web_worker,'platform_context',lambda:service.community)
    summary=web_worker.payment_summary({'id':42,'balance':8,'_stored_method':{'type':3,'id':999}})
    assert summary['local_card']['last4']=='4242' and summary['stored_card']
    env=tenant_env(store,alice,card_vault_key_file=key)
    assert env['PADELWEB_CARD_VAULT_KEY_FILE']==str(key.resolve())
    for public in (summary,env):
        text=json.dumps(public)
        assert PAN not in text and '5555555555554444' not in text
        assert "'cvv'" not in text and '"cvv"' not in text


@pytest.mark.parametrize('outcome',['confirmed','unconfirmed','cvc','bank','uncertain'])
def test_checkout_loads_only_explicit_card_and_never_retries_payment(monkeypatch,tmp_path,outcome):
    from padelbot import booking,payment,worldline_driver
    from padelbot.config import settings
    monkeypatch.setattr(settings,'padelbot_rules_file',tmp_path/'rules.sqlite3')
    events=[]
    monkeypatch.setattr(booking,'add_to_cart',lambda *a,**kw:events.append('cart'))
    monkeypatch.setattr(booking,'create_sale',lambda *a,**kw:events.append('sale') or {'id':12,'total':12})
    monkeypatch.setattr(booking,'fetch_booking_id',lambda *a:55)
    def intent(*args,**kwargs):
        events.append('intent');assert kwargs.get('stored_method_id') is None
        return {'userInitiatedPaymentResult':{'bSuccess':True,'hostedPaymentPageUrl':'https://payment.direct.worldline-solutions.com/session'}}
    monkeypatch.setattr(payment,'create_payment',intent)
    monkeypatch.setattr(payment,'wait_for_payment_confirmation',lambda *a,**kw:outcome=='confirmed')
    monkeypatch.setattr(worldline_driver,'load_card',lambda:pytest.fail('No global/CLI card'))
    def loader():events.append('decrypt');return CARD
    def driver(**kwargs):
        events.append('driver');assert kwargs['card'] is CARD and kwargs['allow_missing_cvv']
        if outcome=='uncertain':return worldline_driver.WorldlineResult(False,error='Unknown submit',submitted=True)
        return worldline_driver.WorldlineResult(outcome in {'confirmed','unconfirmed'},submitted=outcome!='cvc',needed_3ds=outcome=='bank')
    monkeypatch.setattr(worldline_driver,'pay_via_worldline',driver)
    slot=booking.Slot(100,200,'2030-01-01T18:00:00Z','2030-01-01T19:30:00Z',12,{})
    result=booking.book(object(),slot,member_id=42,me={'id':42,'balance':0,'_stored_method':{'id':999,'type':3}},
        expected_amount=Decimal(12),payment_mode='kul',local_card={'network':'visa','expired':False},payment_card_loader=loader)
    assert events==['cart','sale','decrypt','intent','driver']
    assert result.paid is (outcome=='confirmed')
    assert bool(result.payment_url) is (outcome=='cvc')
    assert result.payment_pending is (outcome not in {'confirmed','cvc'})


def test_covering_balance_never_decrypts_or_uses_the_card(monkeypatch,tmp_path):
    from padelbot import booking,payment
    from padelbot.config import settings
    monkeypatch.setattr(settings,'padelbot_rules_file',tmp_path/'rules.sqlite3')
    monkeypatch.setattr(booking,'add_to_cart',lambda *a,**kw:None)
    monkeypatch.setattr(booking,'create_sale',lambda *a,**kw:{'id':12,'total':12})
    monkeypatch.setattr(booking,'fetch_booking_id',lambda *a:55)
    calls=[]
    monkeypatch.setattr(payment,'create_payment',lambda *a,**kw:calls.append(kw) or {})
    monkeypatch.setattr(payment,'wait_for_payment_confirmation',lambda *a,**kw:True)
    slot=booking.Slot(100,200,'2030-01-01T18:00:00Z','2030-01-01T19:30:00Z',12,{})
    result=booking.book(object(),slot,member_id=42,me={'id':42,'balance':12},expected_amount=Decimal(12),payment_mode='kul',
        payment_card_loader=lambda:pytest.fail('No decryption for balance-funded payment'))
    assert result.paid and calls[0]['method']==payment.PayMethod.ACCOUNT


@pytest.mark.parametrize('mode',['no_cvc','cvc','3ds'])
def test_vault_checkout_browser_stops_for_cvc_or_bank_challenge(tmp_path,monkeypatch,mode):
    from padelbot.worldline_driver import pay_via_worldline
    playwright=pytest.importorskip('playwright.sync_api')
    original=playwright.sync_playwright
    submitted=[]
    origin='https://payment.direct.worldline-solutions.com'
    callback='https://usc.kuleuven.cloud/sales/callback?status=success'
    def route_request(route):
        url=route.request.url
        if url==origin+'/form':
            cvc='<input id="payment-cvc" required autocomplete="cc-csc">' if mode=='cvc' else ''
            route.fulfill(content_type='text/html',body=f'''<form action="{origin}/submit" method="post">
                <input id="payment-cardnumber" name="cardnumber"><input id="payment-cardholdername">
                <input name="expiry" autocomplete="cc-exp">{cvc}<input type="submit" id="payment-submit" value="Pay"></form>''')
        elif url==origin+'/submit':
            submitted.append(route.request.post_data)
            body='<iframe src="https://acs.bank.test/challenge"></iframe>' if mode=='3ds' else f'<script>location.href="{callback}"</script>'
            route.fulfill(content_type='text/html',body=body)
        elif url==callback or url=='https://acs.bank.test/challenge':route.fulfill(content_type='text/html',body='Synthetic')
        else:route.abort()
    @contextmanager
    def offline():
        with original() as p:
            launch=p.chromium.launch
            def browser_launch(**kwargs):
                try:browser=launch(**kwargs)
                except playwright.Error as error:
                    if "Executable doesn't exist" in str(error):pytest.skip('Chromium is not installed')
                    raise
                new_context=browser.new_context
                def context(**options):
                    assert options.get('accept_downloads') is False
                    assert not any(k in options for k in ['record_har_path','record_video_dir','storage_state'])
                    result=new_context(**options);result.route('**/*',route_request);return result
                monkeypatch.setattr(browser,'new_context',context)
                return browser
            monkeypatch.setattr(p.chromium,'launch',browser_launch)
            yield p
    monkeypatch.setattr(playwright,'sync_playwright',offline)
    result=pay_via_worldline(payment_url=origin+'/form',card=CARD,allow_missing_cvv=True,timeout_s=3,diagnostics_dir=tmp_path)
    assert result.success is (mode=='no_cvc')
    assert result.submitted is (mode!='cvc')
    assert len(submitted)==(0 if mode=='cvc' else 1)
    assert result.needed_3ds is (mode=='3ds')
    assert not any(PAN.encode() in path.read_bytes() for path in tmp_path.rglob('*') if path.is_file())
