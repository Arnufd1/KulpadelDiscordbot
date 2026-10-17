"""A cancellation stays private, updates canonical state, and is never replayed."""
import asyncio
import json
import time

import pytest

from test_web import portal,sign_in
from test_hub import hub,booking
from test_web_queue import Workers,runner_for,ticks
from padelbot.platform_service import PlatformService
from padelbot.web_runtime import JobRunner


def own_snapshot(store,user,bookings):
    ident=store.enqueue(user['id'],'bookings',{})
    now=time.time()
    with store.conn() as c:
        c.execute("UPDATE jobs SET status='done',started=?,finished=?,result=? WHERE id=?",
            (now,now,json.dumps({'device_verified':True,'bookings':bookings}),ident))
    return ident


def test_cancel_endpoint_requires_private_ownership_csrf_and_accepts_restricted_cleanup(portal):
    app,client,store,(owner,alice,bob)=portal
    actual=booking()
    snapshot=own_snapshot(store,alice,[actual])
    headers=sign_in(client,store,bob)
    assert client.post('/api/bookings/123/cancel',json={},headers=headers).status_code==400
    assert client.get('/api/bookings/cancellations').json()=={'cancellations':{}}
    # Participation in another profile's reservation does not transfer ownership.
    app.state.platform.community.sync(alice['id'],[actual],device_verified=True)
    app.state.platform.community.share(alice['id'],123,True)
    match=app.state.platform.community.list(bob['id'])[0]
    app.state.platform.community.join_verified(match['id'],bob['id'])
    assert client.post('/api/bookings/123/cancel',json={},headers=headers).status_code==400
    headers=sign_in(client,store,alice)
    assert client.post('/api/bookings/123/cancel',json={},headers={'Origin':'http://testserver'}).status_code==403
    store.moderate(owner['id'],alice['id'],restricted=True)
    first=client.post('/api/bookings/123/cancel',json={},headers=headers)
    assert first.status_code==200
    ident=first.json()['job_id']
    assert client.post('/api/bookings/123/cancel',json={},headers=headers).json()['job_id']==ident
    status=client.get('/api/bookings/cancellations').json()['cancellations']['123']
    assert status['status']=='queued' and status['job_id']==ident
    assert client.get('/api/jobs/'+snapshot).json()['result']['bookings'][0]['cancellation']==status
    assert client.post('/api/bookings/0/cancel',json={},headers=headers).status_code==400
    assert client.post('/api/bookings/999999999999999999999/cancel',json={},headers=headers).status_code==400
    assert client.post('/api/bookings/nope/cancel',json={},headers=headers).status_code==422
    store.moderate(owner['id'],alice['id'],suspended=True)
    assert client.post('/api/bookings/123/cancel',json={},headers=headers).status_code in {401,403}


def test_partial_cancel_invalidates_only_target_and_clears_checkout_and_notifications(hub):
    runner,store,community,(_,alice,bob,_)=runner_for(hub)
    service=PlatformService(store,community)
    first=booking();second={**first,'id':124}
    community.sync(alice['id'],[first,second],device_verified=True)
    community.share(alice['id'],123,True);community.share(alice['id'],124,True)
    service.hub.link(alice['id'],500)
    service.queue_payment(alice['id'],55,'https://payment.direct.worldline-solutions.com/private',12,first['start'],booking_id=123)
    service.queue_payment(alice['id'],56,'https://payment.direct.worldline-solutions.com/private',12,second['start'],booking_id=124)
    service.sync_verified_bookings(alice['id'],[first,second],device_verified=True)
    service._queue_notice(alice['id'],f"booked:{alice['id']}:123:future",{'kind':'booked','target':first['start']})
    service._queue_notice(alice['id'],f"reminder:{alice['id']}:123:future",{'kind':'reminder','booking_id':123,'target':first['start']})
    ident=service.enqueue(alice,'cancel_booking',{'booking_id':123})
    runner.finish(ident,'done',{'cancelled':True,'booking_id':123,'refresh_required':True})
    assert [m['id'] for m in community.list(bob['id'])]==[next(m.id for m in community.matches.future_matches() if m.booking_id==124)]
    cancelled=next(m for m in community.matches.future_matches() if m.booking_id==123)
    assert cancelled.status=='cancelled' and not cancelled.hub_confirmed and not cancelled.hub_visible
    with store.conn() as c:
        assert c.execute('SELECT confirmed FROM community_matches WHERE user_id=? AND booking_id=123',(alice['id'],)).fetchone()[0]==0
        assert c.execute('SELECT confirmed FROM booking_notifications WHERE user_id=? AND booking_id=123',(alice['id'],)).fetchone()[0]==0
        assert c.execute('SELECT confirmed FROM booking_notifications WHERE user_id=? AND booking_id=124',(alice['id'],)).fetchone()[0]==1
        target=c.execute('SELECT status,payload FROM private_payments WHERE user_id=? AND booking_id=123',(alice['id'],)).fetchone()
        assert target['status']=='cancelled' and target['payload']==''
        notices=c.execute("SELECT status,payload FROM discord_deliveries WHERE id LIKE ? OR id LIKE ?",
            (f"booked:{alice['id']}:123:%",f"reminder:{alice['id']}:123:%")).fetchall()
        assert all(row['status']=='cancelled' and row['payload']=='' for row in notices)
    assert service.enqueue(alice,'cancel_booking',{'booking_id':123})==ident


def test_cancel_refresh_balance_and_late_account_read_cannot_overwrite_it(hub):
    runner,store,community,(_,alice,_,_)=runner_for(hub)
    service=PlatformService(store,community)
    actual=booking();community.sync(alice['id'],[actual],device_verified=True)
    service.remember_payment(alice['id'],{'balance':8,'direct_debit':False},checked=100)
    old=store.enqueue(alice['id'],'account',{})
    with store.conn() as c:c.execute('UPDATE jobs SET started=? WHERE id=?',(100,old))
    ident=service.enqueue(alice,'cancel_booking',{'booking_id':123})
    runner.finish(ident,'done',{'cancelled':True,'booking_id':123,'device_verified':True,'bookings':[],
        'payment':{'balance':20,'direct_debit':False},'payment_checked_at':200})
    runner.finish(old,'done',{'payment':{'balance':8,'direct_debit':False},'payment_checked_at':100})
    assert service.payment_status(alice['id'])['balance']=='20'
    assert community.list(alice['id'])==[]
    stale=store.enqueue(alice['id'],'bookings',{})
    with store.conn() as c:c.execute('UPDATE jobs SET started=? WHERE id=?',(100,stale))
    runner.finish(stale,'done',{'bookings':[actual],'device_verified':True})
    assert store.job(alice['id'],stale)['result']['bookings']==[]
    with store.conn() as c:
        assert c.execute('SELECT confirmed FROM community_matches WHERE user_id=? AND booking_id=123',(alice['id'],)).fetchone()[0]==0


def test_unknown_cancel_is_not_replayed_or_unlocked_by_later_read_but_safe_failure_can_retry(hub):
    runner,store,community,(_,alice,_,_)=runner_for(hub)
    service=PlatformService(store,community)
    actual=booking();community.sync(alice['id'],[actual],device_verified=True)
    ident=service.enqueue(alice,'cancel_booking',{'booking_id':123})
    with store.conn() as c:c.execute("UPDATE jobs SET status='running' WHERE id=?",(ident,))
    runner.recover()
    assert store.job(alice['id'],ident)['status']=='unknown'
    read=own_snapshot(store,alice,[actual])
    runner.finish(read,'done',{'device_verified':True,'bookings':[actual]})
    assert service.enqueue(alice,'cancel_booking',{'booking_id':123})==ident
    assert service.cancellation_status(alice['id'])['123']['status']=='unknown'
    other={**actual,'id':124};community.sync(alice['id'],[actual,other],device_verified=True)
    own_snapshot(store,alice,[actual,other])
    retryable=service.enqueue(alice,'cancel_booking',{'booking_id':124})
    runner.finish(retryable,'failed',{'error':'Voorcontrole mislukt.','safe_to_retry':True})
    assert '124' not in service.cancellation_status(alice['id'])
    assert service.enqueue(alice,'cancel_booking',{'booking_id':124})!=retryable


def test_cancel_uses_serial_lane_and_worker_uncertainty_is_unknown(hub,monkeypatch):
    async def scenario():
        runner,store,community,(_,alice,_,_)=runner_for(hub)
        workers=Workers(monkeypatch);actual=booking()
        community.sync(alice['id'],[actual],device_verified=True)
        service=PlatformService(store,community)
        ident=service.enqueue(alice,'cancel_booking',{'booking_id':123})
        workers.replies[(ident,'cancel_booking')]=[{'error':'Annulering niet bevestigd.','cancellation_unknown':True}]
        await ticks(runner)
        assert store.job(alice['id'],ident)['status']=='unknown'
        assert workers.calls[0]['kind']=='cancel_booking' and workers.calls[0]['defer']=='false'
        assert service.enqueue(alice,'cancel_booking',{'booking_id':123})==ident
        assert len(workers.calls)==1
    asyncio.run(scenario())


def test_local_sync_error_after_confirmed_cancel_never_invites_retry(hub,monkeypatch):
    runner,store,community,(_,alice,_,_)=runner_for(hub)
    service=PlatformService(store,community);actual=booking()
    community.sync(alice['id'],[actual],device_verified=True)
    ident=service.enqueue(alice,'cancel_booking',{'booking_id':123})
    monkeypatch.setattr(runner.community,'sync',lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('local sync failed')))
    runner.finish(ident,'done',{'cancelled':True,'booking_id':123,'device_verified':True,'bookings':[],
        'payment':{'balance':20,'direct_debit':False},'payment_checked_at':time.time()})
    job=store.job(alice['id'],ident)
    assert job['status']=='done' and job['result']['refresh_required'] is True
    assert service.payment_status(alice['id'])['balance']=='20'
    assert service.enqueue(alice,'cancel_booking',{'booking_id':123})==ident


def test_canonical_discord_ban_blocks_enqueue_and_already_queued_cancel(hub):
    runner,store,community,(_,alice,_,_)=runner_for(hub)
    service=PlatformService(store,community);actual=booking()
    community.sync(alice['id'],[actual],device_verified=True)
    service.hub.link(alice['id'],500)
    ident=service.enqueue(alice,'cancel_booking',{'booking_id':123})
    with community.matches._connection() as c:
        c.execute("INSERT INTO hub_moderation(actor,suspended,restricted,updated) VALUES('discord:500',1,0,?)",(time.time(),))
    assert not store.user(alice['id'])['suspended']
    with pytest.raises(ValueError,match='gepauzeerd'):
        service.enqueue(alice,'cancel_booking',{'booking_id':123})
    with store.conn() as c:row=dict(c.execute('SELECT * FROM jobs WHERE id=?',(ident,)).fetchone())
    assert runner._job_user(row) is None
    assert store.job(alice['id'],ident)['status']=='cancelled'
