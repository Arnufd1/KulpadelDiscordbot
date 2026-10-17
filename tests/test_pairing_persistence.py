"""Registration outlives browser sessions; verified KUL state needs API evidence."""
import json
from pathlib import Path

import pytest

pytest.importorskip('fastapi')
from fastapi.testclient import TestClient
from padelbot.web import WebSettings,create_app
from padelbot.web_runtime import tenant_env,JobRunner


@pytest.fixture
def portal(tmp_path,monkeypatch):
    monkeypatch.setenv('PADELBOT_RULES_FILE',str(tmp_path/'hub.sqlite3'))
    app=create_app(WebSettings(_env_file=None,root=tmp_path/'platform',public_url='http://testserver',secure_cookies=False),start_workers=False)
    store=app.state.store
    claims={'sub':'persistent-google-sub','email':'player@example.test','email_verified':True,'name':'Player'}
    user=store.login_google(claims)
    with TestClient(app) as client:
        token,csrf=store.new_session(user['id']);client.cookies.set('padel_session',token)
        yield app,client,user,claims,{'Origin':'http://testserver','X-CSRF-Token':csrf}


def native_profile(store,user):
    env=tenant_env(store,user)
    path=Path(env['PADELBOT_ANDROID_PROFILE_FILE'])
    path.write_text('{"enrollment_verified":true}')
    return env


def test_native_profile_alone_is_not_a_verified_api_session(portal):
    app,client,user,_,_=portal
    env=native_profile(app.state.store,user)
    state=client.get('/api/me').json()
    assert state['device_ready'] and not state['connected'] and not state['kul_verified']
    Path(env['PADELBOT_TOKEN_FILE']).write_text('opaque-test-token')
    assert not client.get('/api/me').json()['kul_verified']
    app.state.platform.remember_payment(user['id'],{'balance':0,'stored_card':False,'direct_debit':False})
    assert client.get('/api/me').json()['kul_verified']
    Path(env['PADELBOT_ANDROID_FAILURE_FILE']).write_text('{}')
    assert not client.get('/api/me').json()['kul_verified']


def test_missing_session_bootstraps_existing_native_identity_once(portal):
    app,client,user,_,headers=portal
    store=app.state.store;env=native_profile(store,user)
    app.state.platform.remember_payment(user['id'],{'balance':0,'stored_card':False,'direct_debit':False})
    # A cached successful account check must not swallow a required login.
    previous=store.enqueue(user['id'],'account',{})
    JobRunner(store).finish(previous,'done',{'payment':{'balance':0,'stored_card':False,'direct_debit':False}})
    first=client.post('/api/account/check',json={'force':True},headers=headers).json()
    second=client.post('/api/account/check',json={'force':True},headers=headers).json()
    assert first['check']['job_id']==second['check']['job_id']
    job=store.job(user['id'],first['check']['job_id'])
    assert job['kind']=='login' and job['status']=='queued'
    assert Path(env['PADELBOT_ANDROID_PROFILE_FILE']).exists()
    assert not Path(env['PADELBOT_TOKEN_FILE']).exists()


def test_unregistered_or_latched_profile_never_auto_retries_pin(portal):
    app,client,user,_,headers=portal
    response=client.post('/api/account/check',json={},headers=headers).json()
    assert 'Koppel' in response['error'] and not response['checking']
    env=native_profile(app.state.store,user)
    Path(env['PADELBOT_ANDROID_FAILURE_FILE']).write_text('{}')
    response=client.post('/api/account/check',json={'force':True},headers=headers).json()
    assert 'koppeling' in response['error'] and not response['checking']
    with app.state.store.conn() as c:
        assert c.execute('SELECT count(*) FROM jobs').fetchone()[0]==0


def test_reopened_platform_and_new_google_session_keep_identity_paths_and_key(portal):
    app,client,user,claims,_=portal
    store=app.state.store;env=native_profile(store,user)
    key=Path(env['PADELBOT_KEY_FILE']);key.write_bytes(b'existing-key-do-not-rotate')
    Path(env['PADELBOT_ANDROID_PIN_FILE']).write_bytes(b'existing-encrypted-pin')
    from padelbot.web_store import PlatformStore
    reopened=PlatformStore(store.root,'aafmichiels@gmail.com')
    returning=reopened.login_google({**claims,'name':'Renamed Google profile'})
    reopened.new_session(returning['id'])
    new_env=tenant_env(reopened,returning)
    assert returning['id']==user['id']
    for field in ['KEY_FILE','TOKEN_FILE','STORAGE_FILE','ANDROID_PROFILE_FILE','ANDROID_PIN_FILE','ANDROID_EMULATOR_HOME']:
        assert new_env['PADELBOT_'+field]==env['PADELBOT_'+field]
        assert Path(new_env['PADELBOT_'+field]).is_absolute()
    assert key.read_bytes()==b'existing-key-do-not-rotate'
    assert Path(new_env['PADELBOT_ANDROID_PIN_FILE']).read_bytes()==b'existing-encrypted-pin'


def test_worker_rechecks_restriction_before_any_provider_access(portal,monkeypatch):
    app,_,user,_,_=portal;store=app.state.store
    with store.conn() as c:owner=c.execute('SELECT id FROM users WHERE owner=1').fetchone()[0]
    store.moderate(owner,user['id'],restricted=True)
    monkeypatch.setenv('PADELWEB_DB',str(store.path));monkeypatch.setenv('PADELWEB_USER_ID',user['id'])
    from padelbot import web_worker
    monkeypatch.setattr(web_worker,'initialise_key',lambda:pytest.fail('Moderated write reached provider'))
    for kind in ['book','rule','draft_refresh']:
        with pytest.raises(ValueError,match='leestoegang'):
            web_worker.execute(kind,{})
