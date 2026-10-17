import json
import time
from datetime import datetime,timedelta,timezone
from pathlib import Path

import pytest

from test_web import portal,sign_in
from padelbot.matches import MatchError
from padelbot.web_runtime import tenant_env,rules_for


def admin_login(client,store,owner):
    token,csrf=store.new_session(owner['id'])
    ticket=store.admin_ticket(token)
    assert store.verify_admin_ticket(ticket,owner['email'])
    assert store.activate_admin(ticket,token)
    client.cookies.set('padel_session',token)
    return {'Origin':'http://testserver','X-CSRF-Token':csrf}


def test_admin_detail_and_moderation_require_owner_and_tailscale_proof(portal):
    app,client,store,(owner,alice,_)=portal
    path='/api/admin/users/'+alice['id']
    assert client.get(path).status_code==401
    owner_headers=sign_in(client,store,owner)
    forged={**owner_headers,'Tailscale-User-Login':owner['email'],'X-Padel-Tail-Secret':'test-proxy-secret'}
    assert client.get(path,headers=forged).status_code==403
    assert client.patch(path,json={'restricted':True},headers=forged).status_code==403
    alice_headers=sign_in(client,store,alice)
    assert client.get(path).status_code==403
    assert client.patch(path,json={'suspended':True},headers=alice_headers).status_code==403
    headers=admin_login(client,store,owner)
    assert client.get(path).status_code==200
    assert client.patch(path,json={'restricted':True},headers={'Origin':'http://testserver'}).status_code==403
    assert client.patch('/api/admin/users/'+owner['id'],json={'suspended':True},headers=headers).status_code==400
    assert client.patch('/api/admin/users/'+owner['id'],json={'restricted':True},headers=headers).status_code==400


def test_admin_projection_uses_cached_state_redacts_payment_credentials_and_prefers_discord_avatar(portal):
    app,client,store,(owner,alice,bob)=portal
    hub=app.state.community.hub
    google='https://lh3.googleusercontent.com/test-avatar'
    discord='https://cdn.discordapp.com/avatars/123456/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png'
    store.login_google({'sub':'alice','email':alice['email'],'email_verified':True,'name':'alice','picture':google})
    hub.register(store.user(alice['id']));hub.link(alice['id'],123456)
    hub.remember_discord(123456,'Alice Discord',discord)
    hub.remember_guild(987,'Padelvrienden')
    hub.observe_context(123456,'guild',guild_id=987)
    start=(datetime.now(timezone.utc)+timedelta(days=2)).replace(microsecond=0)
    booking={'id':111,'name':'Padel 1','start':start.isoformat(),'end':(start+timedelta(hours=1)).isoformat(),'confirmed':True}
    app.state.community.sync(alice['id'],[booking],device_verified=True)
    app.state.platform.remember_payment(alice['id'],{'balance':'12','direct_debit':False,'stored_method':True,'stored_card':True})
    active=rules_for(store,alice).add_rule(1,'18:00')
    disabled=rules_for(store,alice).add_rule(2,'18:00');rules_for(store,alice).set_enabled(disabled.id,False)
    env=tenant_env(store,alice)
    profile=Path(env['PADELBOT_ANDROID_PROFILE_FILE']);profile.parent.mkdir(parents=True,exist_ok=True)
    profile.write_text(json.dumps({'enrollment_verified':True,'private_secret':'never-expose-native-secret'}))
    Path(env['PADELBOT_TOKEN_FILE']).write_text('never-expose-access-token')
    with store.conn() as c:
        c.execute("INSERT INTO private_payments(user_id,sale_id,booking_id,payload,status,created) VALUES(?,?,?,?,'pending',?)",(alice['id'],77,111,'never-expose-payment-url-or-pan',time.time()))
    headers=admin_login(client,store,owner)
    response=client.get('/api/admin/users/'+alice['id']);assert response.status_code==200
    detail=response.json()
    assert detail['profile']['avatar_url']==discord
    assert detail['discord']['id']=='123456'
    assert detail['discord']['guilds'][0]['name']=='Padelvrienden'
    assert detail['discord']['personal_install']=='unknown'
    assert detail['bookings'][0]['id']==111
    assert detail['bookings_checked'] is not None
    assert [rule['id'] for rule in detail['rules']]==[active.id]
    assert detail['payment']['linked'] is True
    assert detail['pending_payments'][0]['sale_id']==77
    assert set(detail['pending_payments'][0])=={'sale_id','booking_id','status','created'}
    assert not any(secret in response.text for secret in ['never-expose','encrypted_pin','storedPayMethodId','card_number'])
    rows=client.get('/api/admin/users').json()['users']
    assert next(row for row in rows if row['id']==alice['id'])['avatar_url']==discord


def test_restriction_cancels_new_writes_keeps_reads_cleanup_and_leave_available(portal):
    app,client,store,(owner,alice,_)=portal
    token,_=store.new_session(alice['id'])
    book=store.enqueue(alice['id'],'book',{})
    read=store.enqueue(alice['id'],'bookings',{})
    cleanup=store.enqueue(alice['id'],'draft_release',{})
    headers=admin_login(client,store,owner)
    response=client.patch('/api/admin/users/'+alice['id'],json={'restricted':True},headers=headers)
    assert response.status_code==200
    assert response.json()['profile']['restricted'] is True
    assert store.job(alice['id'],book)['status']=='cancelled'
    assert store.job(alice['id'],read)['status']=='queued'
    assert store.job(alice['id'],cleanup)['status']=='queued'
    assert store.session(token) is not None
    for kind in ['book','rule','community_join','draft_refresh']:
        with pytest.raises(ValueError,match='leestoegang'):store.enqueue(alice['id'],kind,{})
    user_headers=sign_in(client,store,store.user(alice['id']))
    assert client.get('/api/me').json()['restricted'] is True
    assert client.get('/api/rules').status_code==200
    assert client.post('/api/rules',json={'day_of_week':2,'time_local':'18:00'},headers=user_headers).status_code==400
    headers=admin_login(client,store,owner)
    assert client.patch('/api/admin/users/'+alice['id'],json={'suspended':True},headers=headers).status_code==200
    assert store.session(token) is None
    assert store.job(alice['id'],cleanup)['status']=='queued'


def test_discord_guests_are_moderated_without_creating_google_accounts(portal):
    app,client,store,(owner,alice,_)=portal
    hub=app.state.community.hub
    hub.remember_discord(4321,'Gastspeler')
    start=datetime.now(timezone.utc)+timedelta(days=1)
    match=hub.matches.create(owner_id=2222,booking_id=1,start_iso=start.isoformat(),end_iso=(start+timedelta(hours=1)).isoformat(),court='Padel',channel_id=123,guild_id=987)
    hub.matches.change_player(match.id,4321,join=True)
    with store.conn() as c:count=c.execute('SELECT count(*) FROM users').fetchone()[0]
    headers=admin_login(client,store,owner)
    guests=client.get('/api/admin/users').json()['guests']
    assert next(row for row in guests if row['id']=='4321')['google_linked'] is False
    guest=client.get('/api/admin/guests/4321').json()
    assert guest['joins'][0]['id']==match.id
    assert client.patch('/api/admin/guests/4321',json={'restricted':True},headers=headers).status_code==200
    hub.matches.change_player(match.id,4321,join=False)
    with pytest.raises(MatchError,match='leestoegang'):hub.matches.change_player(match.id,4321,join=True)
    assert client.patch('/api/admin/guests/4321',json={'suspended':True},headers=headers).status_code==200
    with pytest.raises(MatchError,match='gepauzeerd'):hub.matches.change_player(match.id,4321,join=True)
    hub.register(alice)
    with pytest.raises(MatchError,match='gepauzeerd'):hub.link(alice['id'],4321)
    assert client.patch('/api/admin/guests/4321',json={'suspended':False,'restricted':False},headers=headers).status_code==200
    hub.link(alice['id'],4321)
    assert client.patch('/api/admin/guests/4321',json={'suspended':False},headers=headers).status_code==400
    with store.conn() as c:assert c.execute('SELECT count(*) FROM users').fetchone()[0]==count


def test_guest_restriction_follows_google_link_and_admin_can_clear_all_aliases(portal):
    app,client,store,(owner,alice,_)=portal
    hub=app.state.community.hub
    hub.remember_discord(4321,'Gastspeler');hub.register(alice)
    headers=admin_login(client,store,owner)
    assert client.patch('/api/admin/guests/4321',json={'restricted':True},headers=headers).status_code==200
    hub.link(alice['id'],4321)
    assert client.get('/api/admin/users/'+alice['id']).json()['profile']['restricted'] is True
    assert next(row for row in client.get('/api/admin/users').json()['users'] if row['id']==alice['id'])['restricted'] is True
    user_headers=sign_in(client,store,alice)
    assert client.get('/api/me').json()['restricted'] is True
    with pytest.raises(ValueError,match='leestoegang'):app.state.platform.require_writable(alice['id'])
    start=datetime.now(timezone.utc)+timedelta(days=1)
    booking={'id':111,'name':'Padel','start':start.isoformat(),'end':(start+timedelta(hours=1)).isoformat(),'confirmed':True}
    app.state.community.sync(alice['id'],[booking],device_verified=True)
    # An existing Discord match must not bypass sharing's write guard.
    with hub.matches._connection() as c:
        c.execute("UPDATE hub_moderation SET restricted=0 WHERE actor='discord:4321'")
    app.state.community.share(alice['id'],111,True)
    with hub.matches._connection() as c:
        c.execute("UPDATE hub_moderation SET restricted=1 WHERE actor='discord:4321'")
    app.state.community.share(alice['id'],111,False)
    with pytest.raises(MatchError,match='leestoegang'):app.state.community.share(alice['id'],111,True)
    headers=admin_login(client,store,owner)
    # Updating one flag preserves an existing effective restriction.
    assert client.patch('/api/admin/users/'+alice['id'],json={'suspended':False},headers=headers).json()['profile']['restricted'] is True
    assert client.patch('/api/admin/users/'+alice['id'],json={'restricted':False},headers=headers).json()['profile']['restricted'] is False
    app.state.platform.require_writable(alice['id'])
