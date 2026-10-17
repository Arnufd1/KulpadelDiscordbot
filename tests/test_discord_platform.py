import asyncio
from datetime import datetime,timedelta,timezone
import time

import pytest

from test_web import portal
from tests.test_matches import interaction
from padelbot import discord_platform,match_ui
from padelbot.discord_lifecycle import refresh_due_bookings


def upcoming(ident=123):
    start=datetime.now(timezone.utc)+timedelta(hours=3)
    return {'id':ident,'name':'Padel 1','start':start.isoformat(),'end':(start+timedelta(hours=1)).isoformat(),'confirmed':True}


def test_discord_shares_only_current_profiles_fresh_booking_and_uses_common_roster(portal,monkeypatch):
    app,_,_,(_,alice,bob)=portal;service=app.state.platform
    service.hub.register(alice);service.hub.register(bob)
    service.hub.link(alice['id'],42);service.hub.link(bob['id'],50)
    actual=upcoming();calls=[]
    async def read(service,user,kind,payload):
        calls.append((user['id'],kind))
        service.community.sync(user['id'],[actual],device_verified=True)
        return {'bookings':[actual]}
    monkeypatch.setattr(discord_platform,'job',read)
    async def run():
        it=interaction(service.community.matches)
        await discord_platform.share_booking(it,service,alice,actual['id'])
        assert calls==[(alice['id'],'bookings')]
        assert len(it.replies)==1
        match=service.community.matches.for_booking(42,actual['id'])
        assert match.hub_profile_id==alice['id'] and match.hub_confirmed
        service.community.join_verified(match.id,bob['id'])
        assert service.community.matches.get(match.id).players==(42,50)
        # Sharing to this chat again cannot create a second public message.
        await discord_platform.share_booking(it,service,alice,actual['id'])
        assert len(it.replies)==1
        foreign=interaction(service.community.matches,50)
        with pytest.raises(ValueError):await discord_platform.share_booking(foreign,service,alice,actual['id'])
        assert len(foreign.replies)==0
    asyncio.run(run())


def test_discord_cannot_share_a_different_profiles_booking(portal,monkeypatch):
    app,_,_,(_,alice,bob)=portal;service=app.state.platform
    service.hub.register(alice);service.hub.register(bob)
    service.hub.link(alice['id'],42);service.hub.link(bob['id'],50)
    actual=upcoming()
    service.community.sync(alice['id'],[actual],device_verified=True)
    async def read(service,user,kind,payload):
        assert user['id']==bob['id']
        service.community.sync(user['id'],[],device_verified=True)
        return {'bookings':[]}
    monkeypatch.setattr(discord_platform,'job',read)
    async def run():
        it=interaction(service.community.matches,50)
        await discord_platform.share_booking(it,service,bob,actual['id'])
        assert not it.replies
        assert 'Vernieuw' in it.edits[-1]['content']
    asyncio.run(run())


@pytest.mark.parametrize('active',[True,False])
def test_group_guest_join_checks_host_profile_before_mutation(portal,monkeypatch,active):
    app,_,_,(_,alice,_)=portal;service=app.state.platform
    service.hub.register(alice)
    service.hub.link(alice['id'],42)
    actual=upcoming();service.community.sync(alice['id'],[actual],device_verified=True)
    service.community.share(alice['id'],actual['id'],True)
    match=service.community.matches.for_booking(42,actual['id'])
    post,_=service.community.matches.reserve_post(match.id,42,88,None)
    service.community.matches.bind_post(post.id,88,123)
    calls=[]
    async def read(service,user,kind,payload):
        calls.append(user['id'])
        service.community.sync(user['id'],[actual] if active else [],device_verified=True)
    monkeypatch.setattr(discord_platform,'job',read)
    async def run():
        it=interaction(service.community.matches,90);it.client.platform=service
        view=match_ui.post_view(service.community.matches,match,service.community.matches.get_post(post.id),asyncio.Lock())
        await view.act(it,'join')
        assert calls==[alice['id']]
        assert (90 in service.community.matches.get(match.id).players)==active
    asyncio.run(run())


def test_background_refresh_only_targets_pending_profiles_and_respects_failure_backoff(portal,monkeypatch):
    app,_,store,(_,alice,bob)=portal;service=app.state.platform;now=time.time()
    service.hub.register(alice);service.hub.link(alice['id'],42)
    monkeypatch.setattr(service,'users_needing_booking_refresh',lambda **kwargs:[alice['id']])
    first=refresh_due_bookings(service,now=now)
    assert len(first)==1 and store.job(alice['id'],first[0])['kind']=='bookings'
    assert refresh_due_bookings(service,now=now+121)==[] # active foreground/queued work
    with store.conn() as c:c.execute("UPDATE jobs SET status='failed' WHERE id=?",(first[0],))
    assert refresh_due_bookings(service,now=now+899)==[]
    assert len(refresh_due_bookings(service,now=now+901))==1
    with store.conn() as c:assert c.execute('SELECT count(*) FROM jobs WHERE user_id=?',(bob['id'],)).fetchone()[0]==0


def test_unlinked_profile_only_refreshes_for_pending_website_checkout(portal,monkeypatch):
    app,_,_,(_,alice,_)=portal;service=app.state.platform;now=time.time()
    monkeypatch.setattr(service,'users_needing_booking_refresh',lambda **kwargs:[alice['id']])
    assert refresh_due_bookings(service,now=now)==[]
    service.queue_payment(alice['id'],11,'https://payment.direct.worldline-solutions.com/checkout/own',12,upcoming()['start'])
    assert len(refresh_due_bookings(service,now=now))==1


def test_booking_picker_excludes_unpaid_and_requires_explicit_share(portal):
    app,_,_,(_,alice,_)=portal
    async def run():
        view=discord_platform.BookingsView(app.state.platform,alice,42,[upcoming(),{**upcoming(124),'confirmed':False}])
        assert len(view.select.options)==1 and view.select.options[0].value=='123'
        assert view.share_button.disabled
    asyncio.run(run())


def test_private_booking_result_only_exposes_own_sale_link_and_paid_share(portal):
    app,_,_,(_,alice,bob)=portal;service=app.state.platform
    target=upcoming()['start']
    service.queue_payment(alice['id'],11,'https://payment.direct.worldline-solutions.com/checkout/own',12,target)
    service.queue_payment(bob['id'],12,'https://payment.direct.worldline-solutions.com/checkout/other',12,target)
    async def run():
        view=discord_platform.BookingResultView(service,alice,42,{'sale_id':11,'payment_available':True,'paid':False,'booking_id':123})
        assert [item.url for item in view.children if item.label=='Betalen']==['https://payment.direct.worldline-solutions.com/checkout/own']
        foreign=discord_platform.BookingResultView(service,alice,42,{'sale_id':12,'payment_available':True,'paid':False})
        assert all(item.label!='Betalen' for item in foreign.children)
        paid=discord_platform.BookingResultView(service,alice,42,{'sale_id':11,'paid':True,'booking_id':123})
        assert any(item.label=='Deel in deze chat' for item in paid.children)
    asyncio.run(run())
