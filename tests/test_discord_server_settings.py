import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from test_web import portal, sign_in
from padelbot.discord_server_settings import (
    ADMINISTRATOR, MANAGE_GUILD, VIEW_CHANNEL, SEND_MESSAGES, EMBED_LINKS, SHARE_PERMISSIONS,
    DiscordServerAccess, DiscordServerAccessError, DiscordServerSettings,
    base_permissions, channel_permissions, discord_id,
)
from padelbot.web_store import PlatformStore, google_return_path


GUILD = '1502689920605749359'
ACTOR = '1502689920605749360'
BOT = '1502689920605749361'
MANAGER_ROLE = '1502689920605749362'
BOT_ROLE = '1502689920605749363'
CHANNEL = '1502689920605749364'
OTHER_CHANNEL = '1502689920605749365'
ROUTE = f'/api/discord/servers/{GUILD}/settings'


@pytest.fixture
def discord_rest(monkeypatch):
    guild = {'id': GUILD, 'name': 'Padel vrienden', 'owner_id': '42'}
    actor = {'user': {'id': ACTOR}, 'roles': [MANAGER_ROLE]}
    bot = {'user': {'id': BOT, 'bot': True}, 'roles': [BOT_ROLE]}
    roles = [{'id': GUILD, 'permissions': str(SHARE_PERMISSIONS)},
             {'id': MANAGER_ROLE, 'permissions': str(MANAGE_GUILD)},
             {'id': BOT_ROLE, 'permissions': '0'}]
    channel = {'id': CHANNEL, 'guild_id': GUILD, 'type': 0, 'name': 'padel', 'permission_overwrites': []}
    data = {f'/guilds/{GUILD}': guild, f'/guilds/{GUILD}/members/{ACTOR}': actor,
            f'/guilds/{GUILD}/members/{BOT}': bot, f'/guilds/{GUILD}/roles': roles,
            '/users/@me': {'id': BOT, 'bot': True}, f'/guilds/{GUILD}/channels': [channel]}
    calls = []
    def respond(request):
        assert request.url.host == 'discord.com'
        assert request.headers['Authorization'] == 'Bot private-test-token'
        path = request.url.path.removeprefix('/api/v10')
        calls.append(path)
        response = data.get(path, 404)
        if isinstance(response, Exception):
            raise response
        if type(response) is int:
            return httpx.Response(response, json={'message': 'do not disclose this provider detail'})
        return httpx.Response(200, json=deepcopy(response))
    original_client = httpx.AsyncClient
    monkeypatch.setattr('padelbot.discord_server_settings.httpx.AsyncClient',
                        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs))
    return data, calls


def link_portal(portal):
    app, client, store, (_, alice, _) = portal
    app.state.platform.hub.register(alice)
    app.state.platform.hub.link(alice['id'], int(ACTOR))
    app.state.discord_server_access = DiscordServerAccess('private-test-token')
    return app, client, store, alice, sign_in(client, store, alice)


def test_settings_share_one_database_default_merge_reset_and_audit(tmp_path):
    store = PlatformStore(tmp_path, 'owner@example.test')
    settings = DiscordServerSettings(store)
    assert settings.get(GUILD) == {'share_availability': True, 'share_matches': True, 'share_channel_id': None}
    settings.save(GUILD, {'share_matches': False, 'share_channel_id': CHANNEL}, ACTOR)
    other_process = DiscordServerSettings(PlatformStore(tmp_path, 'owner@example.test'))
    assert other_process.get(int(GUILD)) == {
        'share_availability': True, 'share_matches': False, 'share_channel_id': CHANNEL}
    assert other_process.save(GUILD, {'share_channel_id': None}, ACTOR)['share_matches'] is False
    with store.conn() as connection:
        saved = dict(connection.execute('SELECT * FROM discord_server_settings').fetchone())
        assert saved['updated_by'] == ACTOR and saved['share_channel_id'] is None
        audit = connection.execute("SELECT actor,action,target FROM audit").fetchall()
        assert [tuple(row) for row in audit] == [(ACTOR, 'discord_server_settings_saved', GUILD)] * 2


@pytest.mark.parametrize('value', [True, False, None, -1, 0, '00', '01', '1.0', '1/roles', '1\n', str(2**63), 2**63])
def test_snowflake_ids_reject_unsafe_or_lossy_values(value):
    with pytest.raises(ValueError):
        discord_id(value)


@pytest.mark.parametrize('patch', [{'share_matches': 'false'}, {'share_availability': 0},
                                  {'share_matches': None}, {'anything': True}, {'share_channel_id': True}])
def test_persistence_rejects_weak_types_and_unknown_fields(tmp_path, patch):
    settings = DiscordServerSettings(PlatformStore(tmp_path, 'owner@example.test'))
    with pytest.raises(ValueError):
        settings.save(GUILD, patch, ACTOR)
    assert settings.get(GUILD)['share_matches'] is True


def test_live_guild_membership_and_roles_authorize_no_oauth_tokens(discord_rest):
    data, calls = discord_rest
    result = asyncio.run(DiscordServerAccess('private-test-token').verify(GUILD, ACTOR))
    assert result == {'guild': {'id': GUILD, 'name': 'Padel vrienden'},
                      'channels': [{'id': CHANNEL, 'name': 'padel'}]}
    assert set(calls) == set(data)


@pytest.mark.parametrize('permission', [ADMINISTRATOR, MANAGE_GUILD])
def test_admin_and_manager_permissions_are_valid(discord_rest, permission):
    data, _ = discord_rest
    data[f'/guilds/{GUILD}/roles'][1]['permissions'] = str(permission)
    assert asyncio.run(DiscordServerAccess('private-test-token').verify(GUILD, ACTOR))['guild']['id'] == GUILD


def test_guild_owner_has_management_and_channel_access(discord_rest):
    data, _ = discord_rest
    data[f'/guilds/{GUILD}']['owner_id'] = ACTOR
    data[f'/guilds/{GUILD}/members/{ACTOR}']['roles'] = []
    data[f'/guilds/{GUILD}/roles'][0]['permissions'] = '0'
    data[f'/guilds/{GUILD}/roles'][2]['permissions'] = str(SHARE_PERMISSIONS)
    assert len(asyncio.run(DiscordServerAccess('private-test-token').verify(GUILD, ACTOR))['channels']) == 1


def test_channel_overwrites_combine_roles_before_member_override(discord_rest):
    data, _ = discord_rest
    guild, actor, roles = data[f'/guilds/{GUILD}'], data[f'/guilds/{GUILD}/members/{ACTOR}'], data[f'/guilds/{GUILD}/roles']
    second = '1502689920605749370'
    roles.append({'id': second, 'permissions': '0'})
    actor['roles'].append(second)
    channel = data[f'/guilds/{GUILD}/channels'][0]
    channel['permission_overwrites'] = [
        {'id': GUILD, 'type': 0, 'allow': '0', 'deny': str(VIEW_CHANNEL)},
        {'id': MANAGER_ROLE, 'type': 0, 'allow': '0', 'deny': str(SEND_MESSAGES)},
        {'id': second, 'type': 0, 'allow': str(SEND_MESSAGES | VIEW_CHANNEL), 'deny': '0'},
    ]
    assert channel_permissions(guild, actor, roles, channel) & SHARE_PERMISSIONS == SHARE_PERMISSIONS
    channel['permission_overwrites'].append({'id': ACTOR, 'type': 1, 'allow': '0', 'deny': str(EMBED_LINKS)})
    assert not channel_permissions(guild, actor, roles, channel) & EMBED_LINKS
    # Channel-level MANAGE_GUILD is not a grant of server-management authority.
    roles[1]['permissions'] = '0'
    channel['permission_overwrites'].append({'id': ACTOR, 'type': 1, 'allow': str(MANAGE_GUILD), 'deny': '0'})
    assert not base_permissions(guild, actor, roles) & MANAGE_GUILD


@pytest.mark.parametrize('target,permission', [(ACTOR, VIEW_CHANNEL), (ACTOR, SEND_MESSAGES),
                                               (ACTOR, EMBED_LINKS), (BOT, SEND_MESSAGES), (BOT, EMBED_LINKS)])
def test_channels_hidden_when_either_actor_or_bot_cannot_share(discord_rest, target, permission):
    data, _ = discord_rest
    data[f'/guilds/{GUILD}/channels'][0]['permission_overwrites'] = [
        {'id': target, 'type': 1, 'allow': '0', 'deny': str(permission)}]
    assert asyncio.run(DiscordServerAccess('private-test-token').verify(GUILD, ACTOR))['channels'] == []


def test_timed_out_managers_cannot_select_send_channel(discord_rest):
    data, _ = discord_rest
    data[f'/guilds/{GUILD}/members/{ACTOR}']['communication_disabled_until'] = (
        datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert asyncio.run(DiscordServerAccess('private-test-token').verify(GUILD, ACTOR))['channels'] == []


def test_only_guild_text_and_announcement_channels_can_be_selected(discord_rest):
    data, _ = discord_rest
    template = data[f'/guilds/{GUILD}/channels'][0]
    data[f'/guilds/{GUILD}/channels'] = [{**deepcopy(template), 'id': str(1000 + kind), 'type': kind}
                                      for kind in [0, 1, 2, 4, 5, 10, 11, 12, 15, 16]]
    assert [channel['id'] for channel in asyncio.run(DiscordServerAccess('private-test-token').verify(GUILD, ACTOR))['channels']] == ['1000', '1005']


@pytest.mark.parametrize('broken', ['missing_actor', 'no_manager', 'bot_removed', 'foreign_channel', 'invalid_roles', 'rate_limit', 'network'])
def test_authorization_failures_are_closed_and_sanitized(discord_rest, broken):
    data, _ = discord_rest
    if broken == 'missing_actor':
        data[f'/guilds/{GUILD}/members/{ACTOR}'] = 404
    elif broken == 'no_manager':
        data[f'/guilds/{GUILD}/roles'][1]['permissions'] = '0'
    elif broken == 'bot_removed':
        data[f'/guilds/{GUILD}'] = 403
    elif broken == 'foreign_channel':
        data[f'/guilds/{GUILD}/channels'][0]['guild_id'] = '999'
    elif broken == 'invalid_roles':
        data[f'/guilds/{GUILD}/members/{ACTOR}']['roles'] = ['123']
    elif broken == 'rate_limit':
        data[f'/guilds/{GUILD}/channels'] = 429
    else:
        data[f'/guilds/{GUILD}'] = httpx.ConnectError('private-test-token')
    with pytest.raises(DiscordServerAccessError) as failure:
        asyncio.run(DiscordServerAccess('private-test-token').verify(GUILD, ACTOR))
    assert failure.value.status_code in {403, 503}
    assert 'private-test-token' not in str(failure.value)
    assert 'provider detail' not in str(failure.value)


def test_web_settings_require_google_linked_discord_and_csrf(portal, discord_rest):
    app, client, store, (_, alice, _) = portal
    assert client.get(ROUTE).status_code == 401
    headers = sign_in(client, store, alice)
    assert client.get(ROUTE).status_code == 409
    app.state.platform.hub.link(alice['id'], int(ACTOR))
    app.state.discord_server_access = DiscordServerAccess('private-test-token')
    assert client.patch(ROUTE, json={'share_matches': False}).status_code == 403
    response = client.get(ROUTE)
    assert response.status_code == 200 and response.json()['settings']['share_matches'] is True
    response = client.patch(ROUTE, json={'share_matches': False, 'share_channel_id': CHANNEL}, headers=headers)
    assert response.status_code == 200 and response.json()['settings']['share_matches'] is False
    assert app.state.discord_server_settings.get(GUILD)['share_channel_id'] == CHANNEL
    assert 'private-test-token' not in response.text


def test_every_patch_rechecks_current_roles_and_rejects_revoked_manager(portal, discord_rest):
    data, calls = discord_rest
    app, client, store, alice, headers = link_portal(portal)
    assert client.get(ROUTE).status_code == 200
    data[f'/guilds/{GUILD}/roles'][1]['permissions'] = '0'
    response = client.patch(ROUTE, json={'share_matches': False}, headers=headers)
    assert response.status_code == 403
    assert calls.count(f'/guilds/{GUILD}/members/{ACTOR}') == 2
    assert calls.count(f'/guilds/{GUILD}/roles') == 2
    assert app.state.discord_server_settings.get(GUILD)['share_matches'] is True


def test_web_does_not_trust_channel_id_and_reset_is_available(portal, discord_rest):
    app, client, store, alice, headers = link_portal(portal)
    assert client.patch(ROUTE, json={'share_channel_id': OTHER_CHANNEL}, headers=headers).status_code == 403
    assert client.patch(ROUTE, json={'share_channel_id': CHANNEL}, headers=headers).status_code == 200
    assert client.patch(ROUTE, json={'share_channel_id': None}, headers=headers).json()['settings']['share_channel_id'] is None
    assert app.state.discord_server_settings.get(GUILD)['share_channel_id'] is None


@pytest.mark.parametrize('patch', [{'share_matches': 'false'}, {'share_matches': None}, {'share_channel_id': 123},
                                  {'guild_id': GUILD}, {'actor_id': ACTOR}, {'permissions': '8'}])
def test_web_settings_reject_client_identity_and_weak_patch_types(portal, discord_rest, patch):
    _, client, _, _, headers = link_portal(portal)
    assert client.patch(ROUTE, json=patch, headers=headers).status_code == 422


def test_relinked_identity_during_rest_check_cannot_write(portal):
    app, client, store, alice, headers = link_portal(portal)
    class RelinkDuringCheck:
        async def verify(self, guild_id, actor_id):
            with app.state.platform.hub.matches._connection() as connection:
                connection.execute('UPDATE hub_profiles SET discord_id=99 WHERE id=?', (alice['id'],))
            return {'guild': {'id': GUILD, 'name': 'Group'}, 'channels': []}
    app.state.discord_server_access = RelinkDuringCheck()
    assert client.patch(ROUTE, json={'share_matches': False}, headers=headers).status_code == 409
    assert app.state.discord_server_settings.get(GUILD)['share_matches'] is True


def test_restricted_during_rest_check_cannot_write(portal):
    app, client, store, alice, headers = link_portal(portal)
    class RestrictDuringCheck:
        async def verify(self, guild_id, actor_id):
            store.moderate('owner', alice['id'], restricted=True)
            return {'guild': {'id': GUILD, 'name': 'Group'}, 'channels': []}
    app.state.discord_server_access = RestrictDuringCheck()
    assert client.patch(ROUTE, json={'share_matches': False}, headers=headers).status_code == 400
    assert app.state.discord_server_settings.get(GUILD)['share_matches'] is True
    with store.conn() as connection:
        assert connection.execute('SELECT count(*) FROM discord_server_settings').fetchone()[0] == 0


def test_restricted_and_suspended_profiles_cannot_write_server_settings(portal, discord_rest):
    app, client, store, alice, headers = link_portal(portal)
    store.moderate('owner', alice['id'], restricted=True)
    assert client.patch(ROUTE, json={'share_matches': False}, headers=headers).status_code == 400
    assert app.state.discord_server_settings.get(GUILD)['share_matches'] is True
    store.suspend('owner', alice['id'], True)
    assert client.get(ROUTE).status_code == 401  # suspension also removes all sessions


@pytest.mark.parametrize('destination', [f'/#discord/server/{GUILD}', '/#discord/server/1', f'/#discord/server/{2**63-1}'])
def test_google_server_return_paths_are_exact_and_survive_state(portal, destination):
    _, _, store, _ = portal
    assert google_return_path(destination) == destination
    state, browser, *_ = store.oauth_start(destination)
    assert store.oauth_consume(state, browser)['return_to'] == destination


@pytest.mark.parametrize('destination', ['/#discord/server/0', '/#discord/server/01', '/#discord/server/-1',
                                        f'/#discord/server/{2**63}', f'/#discord/server/{GUILD}/',
                                        f'/#discord/server/{GUILD}?next=https://evil.test',
                                        f'https://evil.test/#discord/server/{GUILD}', f'//#discord/server/{GUILD}'])
def test_invalid_server_login_destinations_cannot_open_redirect(destination):
    assert google_return_path(destination) == '/'


def test_discord_oauth_return_to_server_is_state_bound_and_one_use(portal, monkeypatch):
    app, client, store, (_, alice, _) = portal
    app.state.config.discord_client_id = BOT
    app.state.config.discord_client_secret = 'private-client-secret'
    sign_in(client, store, alice)
    destination = f'/#discord/server/{GUILD}'
    response = client.get('/auth/discord', params={'personal': True, 'next': destination}, follow_redirects=False)
    query = parse_qs(urlsplit(response.headers['location']).query)
    assert query['scope'] == ['identify applications.commands']
    class OAuth:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs): return httpx.Response(200, request=httpx.Request('POST', url), json={'access_token': 'discard-me'})
        async def get(self, url, **kwargs): return httpx.Response(200, request=httpx.Request('GET', url), json={'id': ACTOR, 'username': 'Player'})
    monkeypatch.setattr('padelbot.web.httpx.AsyncClient', lambda **kwargs: OAuth())
    callback = '/auth/discord/callback'
    params = {'state': query['state'][0], 'code': 'code', 'next': 'https://evil.test'}
    result = client.get(callback, params=params, follow_redirects=False)
    assert result.status_code == 303 and result.headers['location'] == 'http://testserver' + destination
    assert app.state.platform.discord_id(alice['id']) == int(ACTOR)
    assert client.get(callback, params=params, follow_redirects=False).status_code == 400
