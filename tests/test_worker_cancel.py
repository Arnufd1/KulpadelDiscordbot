"""Cancellation refreshes use the same authenticated account, never stale data."""
from types import SimpleNamespace
import io
import json

import pytest

from test_hub import hub
from padelbot import booking_manager, client, payment, web_worker


@pytest.fixture
def worker(monkeypatch):
    events=[]

    class Client:
        reads=0
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def me(self):
            self.reads+=1
            events.append(('me',self.reads))
            return {'id':42,'balance':8 if self.reads==1 else 20}

    instance=Client()
    monkeypatch.setattr(client,'BackboneClient',lambda:instance)
    monkeypatch.setattr(web_worker,'initialise_key',lambda:None)
    def cancel(c,*,booking_id,member_id):
        assert c is instance
        events.append(('cancel',booking_id,member_id))
        return {'id':12,'statusCode':2}
    def bookings(c,me):
        assert c is instance
        events.append(('bookings',me['balance']))
        return {'device_verified':True,'bookings':[{'id':456,'start':'2030-10-10T17:00:00Z'}]}
    def stored(c,me,*,cards_only):
        assert cards_only is True
        events.append(('stored',me['balance']))
        return {**me,'_stored_method':{'type':3,'id':99}}
    monkeypatch.setattr(booking_manager,'cancel_booking',cancel)
    monkeypatch.setattr(web_worker,'verified_bookings',bookings)
    monkeypatch.setattr(payment,'attach_stored_method',stored)
    return SimpleNamespace(client=instance,events=events)


def test_cancel_reloads_balance_and_bookings_only_after_provider_confirmation(worker,monkeypatch):
    monkeypatch.setattr(web_worker.time,'time',lambda:1234)
    result=web_worker.execute('cancel_booking',{'booking_id':123})
    assert worker.events==[('me',1),('cancel',123,42),('me',2),('bookings',20)]
    assert result['cancelled'] is True and result['booking_id']==123
    assert result['bookings']==[{'id':456,'start':'2030-10-10T17:00:00Z'}]
    assert result['payment']['balance']==20 and result['payment']['stored_card'] is False
    assert result['payment_checked_at']==1234
    assert 'id' not in result['payment'] and '_stored_method' not in result['payment']


@pytest.mark.parametrize('value',[None,True,'123',-1,0,123.0])
def test_invalid_or_coerced_booking_id_never_reaches_cancel(worker,value):
    result=web_worker.execute('cancel_booking',{'booking_id':value})
    assert result['safe_to_retry'] is True and 'error' in result
    assert worker.events==[('me',1)]


def test_uncertain_cancellation_never_returns_a_refunded_balance_or_removes_booking(worker,monkeypatch):
    def uncertain(*args,**kwargs):
        raise booking_manager.CancellationUncertain('Annulering nog niet bevestigd.')
    monkeypatch.setattr(booking_manager,'cancel_booking',uncertain)
    result=web_worker.execute('cancel_booking',{'booking_id':123})
    assert result=={'error':'Annulering nog niet bevestigd.','booking_id':123,'cancellation_unknown':True}
    assert worker.events==[('me',1)]
    assert not result.get('safe_to_retry') and 'cancelled' not in result


def test_ownership_rejection_is_retryable_only_because_no_write_happened(worker,monkeypatch):
    def rejected(*args,**kwargs):
        raise booking_manager.BookingCancellationError('Deze reservatie is niet meer beschikbaar in je eigen boekingen.')
    monkeypatch.setattr(booking_manager,'cancel_booking',rejected)
    result=web_worker.execute('cancel_booking',{'booking_id':123})
    assert result['safe_to_retry'] is True and 'cancelled' not in result
    assert worker.events==[('me',1)]


@pytest.mark.parametrize('failure',['account','bookings','identity','target_still_present'])
def test_confirmed_cancellation_with_failed_refresh_preserves_known_outcome_without_fake_snapshot(worker,monkeypatch,failure):
    original_me=worker.client.me
    if failure in {'account','identity'}:
        def me():
            if worker.client.reads:
                if failure=='account':raise TimeoutError('private-provider-detail')
                return {'id':43,'balance':999}
            return original_me()
        worker.client.me=me
    else:
        def bookings(*args):
            if failure=='bookings':raise TimeoutError('private-provider-detail')
            return {'device_verified':True,'bookings':[{'id':123}]}
        monkeypatch.setattr(web_worker,'verified_bookings',bookings)
    result=web_worker.execute('cancel_booking',{'booking_id':123})
    assert result['cancelled'] is True and result['refresh_required'] is True
    assert result['booking_id']==123
    assert 'payment' not in result and 'bookings' not in result and 'error' not in result
    assert 'private-provider-detail' not in str(result)
    assert sum(event[0]=='cancel' for event in worker.events)==1


@pytest.mark.parametrize('actor,suspended,restricted',[
    ('profile',True,False),('discord',True,False),('profile',False,True),('discord',False,True),
])
def test_cancel_rechecks_canonical_bans_after_auth_but_allows_restricted_cleanup(worker,hub,monkeypatch,actor,suspended,restricted):
    store,community,(_,alice,_,_)=hub
    community.hub.register(alice)
    community.hub.link(alice['id'],54321)
    assert not store.user(alice['id'])['suspended']
    monkeypatch.setenv('PADELWEB_DB',str(store.path))
    monkeypatch.setenv('PADELWEB_USER_ID',alice['id'])
    monkeypatch.setattr(web_worker,'platform_context',lambda:community)
    original_me=worker.client.me
    key='profile:'+alice['id'] if actor=='profile' else 'discord:54321'
    def me():
        result=original_me()
        if worker.client.reads==1:
            # A canonical moderation change arrives after the queued worker's
            # initial account read, without changing the Google profile row.
            with community.matches._connection() as c:
                c.execute('INSERT INTO hub_moderation(actor,suspended,restricted,updated) VALUES(?,?,?,?)',
                          (key,int(suspended),int(restricted),1))
        return result
    worker.client.me=me
    result=web_worker.execute('cancel_booking',{'booking_id':123})
    if suspended:
        assert result=={'error':'Je profiel is gepauzeerd.','booking_id':123,'safe_to_retry':True}
        assert worker.events==[('me',1)]
    else:
        assert result['cancelled'] is True
        assert ('cancel',123,42) in worker.events


@pytest.mark.parametrize('failure',['initial_account','inside_cancel'])
def test_worker_main_releases_retry_only_when_cancel_domain_was_never_entered(worker,monkeypatch,tmp_path,failure):
    def failed(*args,**kwargs):
        raise TimeoutError('private-provider-detail')
    if failure=='initial_account':
        worker.client.me=failed
    else:
        monkeypatch.setattr(booking_manager,'cancel_booking',failed)
    result_path=tmp_path/'result.json'
    monkeypatch.setenv('PADELWEB_RESULT',str(result_path))
    monkeypatch.setattr(web_worker.signal,'signal',lambda *args:None)
    monkeypatch.setattr(web_worker.sys,'stdin',SimpleNamespace(buffer=io.BytesIO(
        json.dumps({'kind':'cancel_booking','payload':{'booking_id':123}}).encode())))
    web_worker.main()
    result=json.loads(result_path.read_text())
    assert 'error' in result and 'private-provider-detail' not in str(result)
    assert result.get('safe_to_retry',False)==(failure=='initial_account')
    assert 'cancelled' not in result
