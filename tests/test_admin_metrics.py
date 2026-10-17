import json
import time
from types import SimpleNamespace

from test_web import portal,sign_in


def test_metrics_require_owner_and_session_bound_tailscale(portal):
    app,client,store,(owner,alice,_)=portal
    assert client.get('/api/admin/metrics').status_code==401
    sign_in(client,store,alice)
    assert client.get('/api/admin/metrics').status_code==403
    sign_in(client,store,owner)
    assert client.get('/api/admin/metrics').status_code==403
    token,_=store.new_session(owner['id'])
    ticket=store.admin_ticket(token)
    assert store.verify_admin_ticket(ticket,owner['email']) and store.activate_admin(ticket,token)
    client.cookies.set('padel_session',token)
    response=client.get('/api/admin/metrics')
    assert response.status_code==200
    assert response.json()['auth_vm']['status']=='local'
    assert response.json()['cadence']['sample_count']==0


def test_metrics_status_never_wakes_authenticator(portal,monkeypatch,tmp_path):
    from padelbot import config
    app,client,store,(owner,_,_)=portal
    token,_=store.new_session(owner['id']);ticket=store.admin_ticket(token)
    store.verify_admin_ticket(ticket,owner['email']);store.activate_admin(ticket,token)
    client.cookies.set('padel_session',token)
    bridge=tmp_path/'fixed-bridge'
    monkeypatch.setenv('PADELBOT_AUTH_REMOTE_COMMAND',str(bridge))
    calls=[]
    def run(args,**kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0,stdout='{"status":"stopped","leases":0}')
    monkeypatch.setattr('padelbot.web.subprocess.run',run)
    assert client.get('/api/admin/metrics').json()['auth_vm']['status']=='stopped'
    assert calls==[[str(bridge),'status']]
