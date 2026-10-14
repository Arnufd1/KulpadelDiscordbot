import time
from datetime import datetime,timedelta,timezone
from concurrent.futures import ThreadPoolExecutor
import pytest
from padelbot.web_store import PlatformStore
from padelbot.web_community import CommunityBookings
from padelbot.matches import MatchError
from padelbot.hub import HubStore


@pytest.fixture
def hub(tmp_path,monkeypatch):
    monkeypatch.setenv('DISCORD_OWNER_ID','42')
    store=PlatformStore(tmp_path/'platform','owner@example.test')
    users=[store.login_google({'sub':name,'email':email,'email_verified':True,'name':name}) for name,email in
           [('Owner','owner@example.test'),('Alice','alice@example.test'),('Bob','bob@example.test'),('Charlie','charlie@example.test')]]
    community=CommunityBookings(store,hub_path=tmp_path/'rules.sqlite3')
    return store,community,users


def booking():
    start=datetime.now(timezone.utc)+timedelta(days=3)
    return {'id':123,'name':'Padel 1','start':start.isoformat(),'end':(start+timedelta(hours=1)).isoformat(),'confirmed':True}


def test_discord_and_web_really_share_capacity_and_identity(hub):
    _,community,(owner,alice,bob,charlie)=hub
    actual=booking()
    match=community.matches.create(owner_id=42,booking_id=123,start_iso=actual['start'],end_iso=actual['end'],court='Padel 1',channel_id=88,guild_id=77)
    community.matches.change_player(match.id,50,join=True)
    community.sync(owner['id'],[actual],device_verified=True)
    code=community.hub.new_code(alice['id'])
    community.hub.consume_code(code,50)
    assert community.list(alice['id'])[0]['joined'] is True
    assert community.list(alice['id'])[0]['count']==2
    community.join_verified(match.id,alice['id'])
    community.join_verified(match.id,bob['id'])
    community.matches.change_player(match.id,51,join=True)
    assert len(community.matches.get(match.id).players)==4
    with pytest.raises(ValueError,match='volzet'):community.join_verified(match.id,charlie['id'])
    with pytest.raises(MatchError,match='vol'):community.matches.change_player(match.id,52,join=True)
    community.leave(match.id,alice['id'])
    assert 50 not in community.matches.get(match.id).players
    assert community.list(bob['id'])[0]['count']==3


def test_link_codes_are_one_use_and_cannot_take_another_identity(hub):
    _,community,(_,alice,bob,_)=hub
    code=community.hub.new_code(alice['id'])
    with community.matches._connection() as c:
        assert code not in str(c.execute('SELECT * FROM hub_link_codes').fetchall())
    community.hub.consume_code(code,50)
    with pytest.raises(ValueError):community.hub.consume_code(code,51)
    with pytest.raises(ValueError):community.hub.link(bob['id'],50)
    with pytest.raises(ValueError):community.hub.link(alice['id'],51)
    assert community.hub.linked(alice['id']) and not community.hub.linked(bob['id'])


def test_linking_merges_existing_two_seats_without_losing_other_players(hub):
    _,community,(owner,alice,bob,_)=hub
    actual=booking()
    community.sync(owner['id'],[actual],device_verified=True)
    community.share(owner['id'],123,True)
    match=community.list(bob['id'])[0]
    community.matches.change_player(match['id'],50,join=True)
    community.join_verified(match['id'],alice['id'])
    assert len(community.matches.get(match['id']).players)==3
    community.hub.link(alice['id'],50)
    assert len(community.matches.get(match['id']).players)==2
    assert community.list(alice['id'])[0]['joined']


def test_mixed_origin_concurrent_joins_never_exceed_four(hub):
    _,community,(owner,alice,bob,charlie)=hub
    actual=booking()
    community.sync(owner['id'],[actual],device_verified=True)
    community.share(owner['id'],123,True)
    match=community.list(alice['id'])[0]
    def attempt(value):
        try:
            if isinstance(value,int):community.matches.change_player(match['id'],value,join=True)
            else:community.join_verified(match['id'],value['id'])
            return True
        except (ValueError,MatchError):return False
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(attempt,[alice,bob,charlie,50,51,52]))==3
    assert len(community.matches.get(match['id']).players)==4


def test_web_names_are_rendered_as_text_in_discord(hub):
    from padelbot.match_ui import match_embed
    _,community,(owner,alice,_,_)=hub
    actual=booking()
    community.sync(owner['id'],[actual],device_verified=True)
    community.share(owner['id'],123,True)
    match=community.list(alice['id'])[0]
    community.join_verified(match['id'],alice['id'])
    embed=match_embed(community.matches.get(match['id']))
    assert 'Alice' in embed.fields[0].value and '<@web:' not in embed.fields[0].value
