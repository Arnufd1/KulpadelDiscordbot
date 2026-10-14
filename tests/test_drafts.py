import copy,time
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import pytest
from test_hub import hub
from padelbot.drafts import DraftStore,draft_deadline,PERIOD,ensure_draft_cart
from padelbot.booking import Slot
from padelbot.cart import CartStateUnknownError


def proposal(community,profile,hours=72):
    start=datetime.now(timezone.utc)+timedelta(hours=hours)
    end=start+timedelta(hours=1)
    drafts=DraftStore(community)
    row=drafts.create(profile['id'],100,123,start.isoformat(),end.isoformat())
    slot=Slot(100,123,row['target'],row['end'],None,{})
    return drafts,row,slot


class Cart:
    def __init__(self):self.items=[];self.writes=[];self.lose_response=False
    def get(self,path,params=None):
        assert path=='/cart-items'
        return {'data':copy.deepcopy(self.items),'total':len(self.items)}
    def post(self,path,body):
        assert path=='/cart-items', 'Draft must never create sales/payments'
        self.writes.append(path)
        self.items.append({**copy.deepcopy(body),'id':len(self.items)+1})
        if self.lose_response:raise TimeoutError('response lost')
        return self.items[-1]
    def delete(self,path):
        assert path.startswith('/cart-items/')
        self.writes.append(path)
        self.items=[i for i in self.items if i['id']!=int(path.rsplit('/',1)[1])]


@pytest.mark.parametrize('hours,expected',[(72,24*3600),(48,6*3600),(24,6*3600),(16,PERIOD),(8,PERIOD)])
def test_duration_tiers_are_exact_multiples_of_45_minutes(hours,expected):
    created=1_800_000_000
    assert draft_deadline(created,created+hours*3600,created)-created==expected
    assert expected%PERIOD==0


def test_a_stricter_boundary_never_extends_a_draft():
    created=1_800_000_000
    assert draft_deadline(created,created+50*3600,created+2*3600)==created+6*3600
    assert draft_deadline(created,created+20*3600,created+4*3600)==created+PERIOD


def test_cart_reuse_and_lost_response_never_create_a_sale_or_duplicate(hub):
    _,community,(owner,_,_,_)=hub
    drafts,row,slot=proposal(community,owner)
    client=Cart();client.lose_response=True
    label='Sigmadvisor draft '+row['id']
    item=ensure_draft_cart(client,slot,42,1,label)
    assert item['id']==1 and client.writes==['/cart-items']
    assert ensure_draft_cart(client,slot,42,1,label)['id']==1
    assert client.writes==['/cart-items']
    drafts.activate(row['id'],slot,item,'Padel 1')
    assert drafts.list(owner['id'])[0]['kind']=='draft'
    assert drafts.community.list(owner['id'])==[]


def test_draft_wont_replace_foreign_cart_item(hub):
    _,community,(owner,_,_,_)=hub
    _,row,slot=proposal(community,owner)
    client=Cart()
    ensure_draft_cart(client,slot,42,1,'Handmatig item')
    before=copy.deepcopy(client.items)
    with pytest.raises(ValueError):ensure_draft_cart(client,slot,42,1,'Sigmadvisor draft '+row['id'])
    assert client.items==before and client.writes==['/cart-items']


def test_renew_expire_and_release_only_our_exact_item(hub,monkeypatch):
    _,community,(owner,_,_,_)=hub
    now=time.time();clock=[now]
    monkeypatch.setattr('padelbot.drafts.time.time',lambda:clock[0])
    drafts,row,slot=proposal(community,owner,8)
    client=Cart()
    item=ensure_draft_cart(client,slot,42,1,'Sigmadvisor draft '+row['id'])
    drafts.activate(row['id'],slot,item,'Padel 1')
    other={**copy.deepcopy(item),'id':99,'params':{**item['params'],'startDate':'2035-01-01T18:00:00Z'}}
    client.items.append(other)
    clock[0]=now+PERIOD
    due=drafts.due()
    assert due[0]['kind']=='draft_release'
    assert drafts.list(owner['id'])==[]
    drafts.release(client,row['id'],42)
    assert [i['id'] for i in client.items]==[99]
    assert drafts.get(row['id'])['state']=='expired'
    with pytest.raises(ValueError):drafts.create(owner['id'],100,123,row['target'],row['end'])


def test_no_automatic_replay_after_uncertain_write_or_restart(hub,monkeypatch):
    _,community,(owner,_,_,_)=hub
    drafts,row,slot=proposal(community,owner)
    drafts.recover()
    assert drafts.get(row['id'])['state']=='uncertain'
    assert drafts.due()==[]
    assert drafts.list(owner['id'])==[]


def test_draft_players_are_atomic_and_organiser_confirmation_is_separate(hub):
    _,community,(owner,alice,bob,charlie)=hub
    drafts,row,slot=proposal(community,owner)
    drafts.activate(row['id'],slot,{'id':1},'Padel 1')
    for player in (alice,bob,charlie):drafts.joined(row['id'],player['id'])
    assert drafts.list(alice['id'])[0]['count']==4
    assert drafts.get(row['id'])['state']=='active'
    with pytest.raises(ValueError):drafts.start_conversion(row['id'],alice['id'])
    drafts.start_conversion(row['id'],owner['id'])
    with pytest.raises(ValueError):drafts.stop(row['id'],owner['id'])
    drafts.converted(row['id'],765,True)
    assert drafts.list(owner['id'])==[]
    assert community.list(alice['id'])[0]['count']==4
    assert community.list(alice['id'])[0]['kind']=='confirmed'


def test_worker_checks_cart_without_creating_sale_and_cleanup_never_repeats_forever(hub,tmp_path,monkeypatch):
    from padelbot import web_worker,client as client_module,booking
    _,community,(owner,_,_,_)=hub
    drafts,row,slot=proposal(community,owner)
    class Client(Cart):
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def me(self):return {'id':42,'category':1}
    client=Client()
    monkeypatch.setattr(client_module,'BackboneClient',lambda:client)
    monkeypatch.setattr(web_worker,'initialise_key',lambda:None)
    monkeypatch.setattr(web_worker,'platform_context',lambda:community)
    monkeypatch.setattr(booking,'find_slots',lambda *a,**k:[slot])
    monkeypatch.setattr(booking,'get_product_names',lambda *a,**k:{100:'Padel 1'})
    profile=tmp_path/'registered';profile.touch()
    monkeypatch.setattr(web_worker,'settings',SimpleNamespace(padelbot_android_profile_file=profile,padelbot_rules_file=tmp_path/'worker-rules.sqlite3'))
    monkeypatch.setenv('PADELWEB_USER_ID',owner['id'])
    assert web_worker.execute('draft_refresh',{'draft_id':row['id']})['draft_id']==row['id']
    assert client.writes==['/cart-items']
    assert drafts.get(row['id'])['state']=='active'
    drafts.stop(row['id'],owner['id'])
    clock=[time.time()]
    monkeypatch.setattr('padelbot.drafts.time.time',lambda:clock[0])
    for _ in range(3):
        assert drafts.due()[0]['kind']=='draft_release'
        clock[0]+=301
    assert drafts.due()==[] and drafts.get(row['id'])['state']=='cleanup_failed'


def test_restart_during_payment_never_replays_and_timezone_cannot_restart_quota(hub):
    _,community,(owner,_,_,_)=hub
    drafts,row,slot=proposal(community,owner)
    drafts.activate(row['id'],slot,{'id':1},'Padel 1')
    drafts.start_conversion(row['id'],owner['id'])
    drafts.recover()
    assert drafts.get(row['id'])['state']=='payment_pending' and drafts.due()==[]
    with pytest.raises(ValueError):drafts.start_conversion(row['id'],owner['id'])
    equivalent=datetime.fromisoformat(row['target']).astimezone(timezone(timedelta(hours=2))).isoformat()
    with pytest.raises(ValueError,match='looptijd'):drafts.create(owner['id'],100,123,equivalent,row['end'])


def test_provider_timestamp_formatting_after_conversion_preserves_roster(hub):
    _,community,(owner,alice,_,_)=hub
    drafts,row,slot=proposal(community,owner)
    drafts.activate(row['id'],slot,{'id':1},'Padel 1')
    drafts.joined(row['id'],alice['id'])
    drafts.start_conversion(row['id'],owner['id']);drafts.converted(row['id'],765,True)
    community.sync(owner['id'],[{'id':765,'name':'Padel 1','start':row['target'].replace('+00:00','Z'),'end':row['end'].replace('+00:00','Z'),'confirmed':True}],device_verified=True)
    assert community.list(alice['id'])[0]['count']==2


def test_killed_worker_does_not_leave_pending_or_converting_drafts_stuck(hub):
    from padelbot.web_runtime import JobRunner
    store,community,(owner,_,_,_)=hub
    drafts,row,slot=proposal(community,owner)
    runner=JobRunner(store);runner.community=community;runner.drafts=drafts
    job=store.enqueue(owner['id'],'draft_refresh',{'draft_id':row['id']})
    runner.finish(job,'unknown',{'error':'Worker interrupted'})
    assert drafts.get(row['id'])['state']=='uncertain'
    # A confirmed write that was interrupted cannot be automatically retried.
    with drafts.matches._connection() as c:c.execute("UPDATE hub_drafts SET state='active' WHERE id=?",(row['id'],))
    drafts.start_conversion(row['id'],owner['id'])
    job=store.enqueue(owner['id'],'book',{'draft_id':row['id']})
    runner.finish(job,'unknown',{'error':'Worker interrupted'})
    assert drafts.get(row['id'])['state']=='payment_pending'
    assert drafts.due()==[]
