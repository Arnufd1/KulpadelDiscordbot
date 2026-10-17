"""Server settings destinations are bound to an authenticated OAuth attempt."""
import time

import pytest

from test_web import portal
from padelbot.platform_service import PlatformService
from padelbot.web_store import digest


def test_discord_server_destination_is_session_user_and_one_use_bound(portal):
    app, _, _, (_, alice, bob) = portal
    service = app.state.platform
    route = '/#discord/server/987654321012345678'
    state = service.oauth_start(alice['id'], 'browser-session', route)
    assert service.oauth_consume(state, 'different-session', alice['id']) is None
    assert service.oauth_consume(state, 'browser-session', bob['id']) is None
    flow = service.oauth_consume(state, 'browser-session', alice['id'])
    assert flow['return_to'] == route
    assert service.oauth_consume(state, 'browser-session', alice['id']) is None


@pytest.mark.parametrize('route', ['https://evil.test/#discord/server/123',
    '//evil.test', '/#discord/server/123/anything', '/#discord/server/0',
    '/#discord/server/9223372036854775808', '/#discord/server/123?next=https://evil.test'])
def test_discord_settings_oauth_rejects_unrelated_or_invalid_destinations(portal, route):
    app, _, _, (_, alice, _) = portal
    service = app.state.platform
    state = service.oauth_start(alice['id'], 'session', route)
    assert service.oauth_consume(state, 'session', alice['id'])['return_to'] == '/#dashboard'


def test_legacy_discord_oauth_migration_keeps_pending_login_and_defaults_dashboard(portal):
    app, _, store, (_, alice, _) = portal
    with store.conn() as connection:
        connection.execute('DROP TABLE discord_oauth')
        connection.execute('CREATE TABLE discord_oauth(state TEXT PRIMARY KEY,session TEXT NOT NULL,user_id TEXT NOT NULL,expires REAL NOT NULL)')
        connection.execute('INSERT INTO discord_oauth VALUES(?,?,?,?)',
            (digest('old-state'), digest('session'), alice['id'], time.time()+600))
    service = PlatformService(store, app.state.community)
    flow = service.oauth_consume('old-state', 'session', alice['id'])
    assert flow['return_to'] == '/#dashboard'
    state = service.oauth_start(alice['id'], 'session')
    assert service.oauth_consume(state, 'session', alice['id'])['return_to'] == '/#dashboard'
