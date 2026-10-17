import asyncio
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import time

import pytest

from padelbot.hub import HubStore
from padelbot.identity import (google_avatar,discord_image,discord_avatar,
    remember_interaction,refresh_known_discord_users)
from padelbot.matches import MatchStore,MatchError


@pytest.fixture
def hub(tmp_path):
    return HubStore(MatchStore(tmp_path/'hub.sqlite3'))


def person(hub,profile_id='a'*32,discord_id=None):
    user={'id':profile_id,'name':'Alice Example','avatar_url':'https://lh3.googleusercontent.com/a/alice'}
    hub.register(user)
    if discord_id:hub.link(profile_id,discord_id)
    return user


def future_match(hub):
    start=datetime.now(timezone.utc)+timedelta(days=1)
    return hub.matches.create(owner_id=100,booking_id=123,start_iso=start.isoformat(),
        end_iso=(start+timedelta(hours=1)).isoformat(),court='Padel',channel_id=None,guild_id=None)


@pytest.mark.parametrize('value',[
    'http://lh3.googleusercontent.com/a/picture',
    'https://lh3.googleusercontent.com.evil.test/a/picture',
    'https://evil.test/a/picture',
    'https://secret@lh3.googleusercontent.com/a/picture',
    'https://lh3.googleusercontent.com:444/a/picture',
    'https://lh3.googleusercontent.com/a/picture#fragment',
    'https://lh3.googleusercontent.com/a/\npicture',
])
def test_google_avatar_refuses_untrusted_or_ambiguous_urls(value):
    assert google_avatar(value) is None


def test_discord_avatar_only_uses_provider_paths():
    avatar=discord_avatar(12345,'a'*32)
    assert avatar=='https://cdn.discordapp.com/avatars/12345/'+('a'*32)+'.png?size=128'
    assert discord_image(avatar)==avatar
    assert discord_image('https://cdn.discordapp.com/attachments/1/picture.png') is None
    assert discord_image('https://cdn.discordapp.com.evil.test/avatars/123/a.png') is None
    assert discord_avatar(12345,None).startswith('https://cdn.discordapp.com/embed/avatars/')


def test_linked_discord_profile_and_avatar_take_precedence_and_survive_reopen(hub):
    user=person(hub,discord_id=12345)
    avatar=discord_avatar(12345,'a'*32)
    hub.remember_discord(12345,'Actual Discord Name',avatar)
    reopened=HubStore(MatchStore(hub.matches.path))
    identity=reopened.profile_identity(user['id'])
    assert identity['registered'] and identity['discord_id']==12345
    assert identity['name']=='Actual Discord Name' and identity['avatar_url']==avatar
    assert reopened.discord_profile(12345)['avatar']==avatar


def test_unlinked_profile_uses_verified_google_avatar(hub):
    user=person(hub)
    assert hub.profile_identity(user['id'])['avatar_url']==user['avatar_url']


def test_discord_guest_has_no_google_profile_and_merges_one_seat_when_linked(hub):
    match=future_match(hub)
    avatar=discord_avatar(12345,'b'*32)
    hub.remember_discord(12345,'Guest Name',avatar)
    hub.matches.change_player(match.id,12345,join=True)
    guest=hub.player_identity(12345)
    assert guest['registered'] is False and guest['name']=='Guest Name' and guest['avatar']==avatar
    with hub.matches._connection() as c:
        assert c.execute('SELECT count(*) FROM hub_profiles').fetchone()[0]==0
    user=person(hub,discord_id=12345)
    assert hub.player_identity(12345)['registered'] is True
    assert hub.player_identity(12345)['id']==user['id']
    assert hub.matches.get(match.id).players==(100,12345)


def test_observed_personal_context_does_not_invent_a_server_install(hub):
    user=SimpleNamespace(id=12345,bot=False,display_name='Real Name',display_avatar=None,avatar=None)
    interaction=SimpleNamespace(user=user,guild_id=999,guild=SimpleNamespace(id=999,name='Some server'),
        is_user_integration=lambda:True,is_guild_integration=lambda:False)
    remember_interaction(hub,interaction)
    identity=hub.discord_profile(12345)
    assert {c['context'] for c in identity['contexts']}=={'personal','guild'}
    assert identity['guilds']==[] and hub.guilds()==[]
    interaction.is_guild_integration=lambda:True
    remember_interaction(hub,interaction)
    assert hub.guilds()[0]['name']=='Some server'
    assert hub.discord_profile(12345)['guilds'][0]['id']==999
    hub.sync_guilds([])
    assert not hub.guilds()[0]['active']


def test_known_identity_refresh_is_bounded_and_never_sends_messages(hub):
    for discord_id in range(1,40):hub.remember_discord(discord_id,'Cached')
    with hub.matches._connection() as c:c.execute('UPDATE hub_discord_names SET seen_at=0')
    seen=[]
    class Bot:
        def get_user(self,user_id):return None
        async def fetch_user(self,user_id):
            seen.append(user_id)
            return SimpleNamespace(id=user_id,bot=False,display_name='Verified Name',avatar=None,display_avatar=None)
    asyncio.run(refresh_known_discord_users(Bot(),hub))
    assert len(seen)==30 and len(set(seen))==30
    assert hub.discord_profile(seen[0])['name']=='Verified Name'


def test_published_post_backfills_known_context_without_claiming_personal_install(hub):
    start=datetime.now(timezone.utc)+timedelta(days=1)
    match=hub.matches.create(owner_id=100,booking_id=123,start_iso=start.isoformat(),
        end_iso=(start+timedelta(hours=1)).isoformat(),court='Padel',channel_id=123,guild_id=999)
    hub.remember_guild(999,'Known bot server')
    hub.backfill_shared_contexts()
    assert hub.discord_profile(100)['contexts']==[]  # A pending send proves nothing.
    hub.matches.bind_message(match.id,123,555)
    hub.backfill_shared_contexts()
    identity=hub.discord_profile(100)
    assert [row['context'] for row in identity['contexts']]==['guild']
    assert identity['guilds'][0]['name']=='Known bot server'
    hub.observe_context(100,'guild',guild_id=999)
    newer=hub.discord_profile(100)['contexts'][0]['last_seen']
    hub.backfill_shared_contexts()
    assert hub.discord_profile(100)['contexts'][0]['last_seen']==newer


@pytest.mark.parametrize('flag',['suspended','restricted'])
def test_guest_moderation_blocks_new_joins_and_follows_linked_identity(hub,flag):
    match=future_match(hub)
    with hub.matches._connection() as c:
        c.execute(f'INSERT INTO hub_moderation(actor,{flag},updated) VALUES(?,1,?)',('discord:12345',time.time()))
    with pytest.raises(MatchError):hub.matches.change_player(match.id,12345,join=True)
    user=person(hub)
    if flag=='suspended':
        with pytest.raises(MatchError):hub.link(user['id'],12345)
    else:
        hub.link(user['id'],12345)
        with hub.matches._connection() as c:
            assert hub.actor(c,user['id'])==12345
            with pytest.raises(MatchError):hub.actor(c,user['id'],write=True)
    assert hub.matches.get(match.id).players==(100,)


def test_readonly_player_can_leave_but_cannot_rejoin(hub):
    match=future_match(hub)
    hub.matches.change_player(match.id,12345,join=True)
    with hub.matches._connection() as c:
        c.execute('INSERT INTO hub_moderation(actor,restricted,updated) VALUES(?,1,?)',('discord:12345',time.time()))
    hub.matches.change_player(match.id,12345,join=False)
    with pytest.raises(MatchError):hub.matches.change_player(match.id,12345,join=True)
