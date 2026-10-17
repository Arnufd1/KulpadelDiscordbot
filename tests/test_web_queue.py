"""The HTTP lanes never hold the shared authenticator or replay a write."""
import asyncio
from collections import defaultdict
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_hub import hub,booking
from padelbot.web_runtime import JobRunner
from padelbot.web_store import READ_JOB_KINDS


class Workers:
    def __init__(self,monkeypatch):
        self.calls=[];self.active={};self.gates={};self.replies=defaultdict(list)
        monkeypatch.setattr('padelbot.web_runtime.asyncio.create_subprocess_exec',self.spawn)

    async def spawn(self,*args,env,**kwargs):
        owner=self
        class Process:
            returncode=None
            async def communicate(self,message):
                command=json.loads(message);key=(env['PADELWEB_JOB_ID'],command['kind'])
                call={**command,'user_id':env['PADELWEB_USER_ID'],'job_id':key[0],
                      'defer':env['PADELWEB_DEFER_AUTH'],'context':env['PADELWEB_AUTH_CONTEXT']}
                owner.calls.append(call);owner.active[key]=call
                try:
                    if key in owner.gates:await owner.gates[key].wait()
                    result=owner.replies[key].pop(0) if owner.replies[key] else {'ok':True}
                    Path(env['PADELWEB_RESULT']).write_text(json.dumps(result),encoding='utf-8')
                    self.returncode=0
                    return b'',b''
                finally:owner.active.pop(key,None)
            def terminate(self):self.returncode=-15
            def kill(self):self.returncode=-9
            async def wait(self):return self.returncode
        return Process()


def runner_for(hub):
    store,community,users=hub
    runner=JobRunner(store);runner.community=community
    from padelbot.drafts import DraftStore
    runner.drafts=DraftStore(community)
    return runner,store,community,users


async def ticks(runner,count=12):
    for _ in range(count):
        await runner.tick()
        await asyncio.sleep(0)


def test_two_read_lanes_progress_while_serial_lane_is_busy_and_keep_profile_order(hub,monkeypatch):
    async def scenario():
        runner,store,_,(_,alice,bob,charlie)=runner_for(hub)
        dave=store.login_google({'sub':'d','email':'d@example.test','email_verified':True})
        workers=Workers(monkeypatch)
        a=store.enqueue(alice['id'],'slots',{})
        a_write=store.enqueue(alice['id'],'book',{})
        b=store.enqueue(bob['id'],'bookings',{})
        c=store.enqueue(charlie['id'],'login',{})
        d=store.enqueue(dave['id'],'slots',{})
        for key in [(a,'slots'),(b,'bookings'),(c,'login'),(d,'slots')]:workers.gates[key]=asyncio.Event()
        await ticks(runner)
        assert set(workers.active)=={(a,'slots'),(b,'bookings'),(c,'login')}
        assert store.job(alice['id'],a_write)['status']=='queued'
        assert store.job(dave['id'],d)['status']=='queued'
        workers.gates[(a,'slots')].set();await ticks(runner)
        assert (d,'slots') in workers.active  # Slow login does not block unrelated HTTP reads.
        assert store.job(alice['id'],a_write)['status']=='queued'
        workers.gates[(c,'login')].set();await ticks(runner)
        assert store.job(alice['id'],a_write)['status']=='done'
        for gate in workers.gates.values():gate.set()
        await ticks(runner)
        assert all(call['defer']=='true' for call in workers.calls if call['kind'] in READ_JOB_KINDS)
        assert all(call['defer']=='false' for call in workers.calls if call['kind'] not in READ_JOB_KINDS)
    asyncio.run(scenario())


def test_deferred_read_uses_same_capacity_slot_and_resumes_exactly_once(hub,monkeypatch):
    async def scenario():
        runner,store,_,(_,alice,bob,_)=runner_for(hub);workers=Workers(monkeypatch)
        first=store.enqueue(alice['id'],'slots',{})
        store.enqueue(alice['id'],'account',{});store.enqueue(alice['id'],'quote',{})
        unrelated=store.enqueue(bob['id'],'slots',{})
        workers.replies[(first,'slots')]=[{'reauth_required':True},{'slots':[]}]
        workers.gates[(first,'login')]=asyncio.Event()
        await ticks(runner)
        assert store.job(alice['id'],first)['status']=='waiting_auth'
        assert store.job(bob['id'],unrelated)['status']=='done'
        with store.conn() as c:
            assert c.execute('SELECT count(*) FROM jobs WHERE user_id=?',(alice['id'],)).fetchone()[0]==3
        with pytest.raises(ValueError):store.enqueue(alice['id'],'slots',{})
        workers.gates[(first,'login')].set();await ticks(runner,15)
        assert store.job(alice['id'],first)['status']=='done'
        calls=[call['kind'] for call in workers.calls if call['job_id']==first]
        assert calls==['slots','login','slots']
        assert next(call for call in workers.calls if call['job_id']==first and call['kind']=='login')['context']=='renewal'
        with store.conn() as c:assert c.execute('SELECT reauth_attempts FROM jobs WHERE id=?',(first,)).fetchone()[0]==1
        assert (store.root/'telemetry.sqlite3').exists()
    asyncio.run(scenario())


def test_existing_login_bypasses_followers_and_is_coalesced(hub,monkeypatch):
    async def scenario():
        runner,store,_,(_,alice,_,_)=runner_for(hub);workers=Workers(monkeypatch)
        first=store.enqueue(alice['id'],'slots',{})
        follower=store.enqueue(alice['id'],'bookings',{})
        login=store.enqueue(alice['id'],'login',{})
        workers.replies[(first,'slots')]=[{'reauth_required':True},{'slots':[]}]
        await ticks(runner,20)
        assert [call['kind'] for call in workers.calls]==['slots','login','slots','bookings']
        assert all(store.job(alice['id'],ident)['status']=='done' for ident in [first,follower,login])
        with store.conn() as c:assert c.execute('SELECT count(*) FROM jobs').fetchone()[0]==3
    asyncio.run(scenario())


@pytest.mark.parametrize('failed_login',[False,True])
def test_no_auth_retry_loop_and_no_replay_of_a_mutation(hub,monkeypatch,failed_login):
    async def scenario():
        runner,store,_,(_,alice,bob,_)=runner_for(hub);workers=Workers(monkeypatch)
        read=store.enqueue(alice['id'],'slots',{})
        write=store.enqueue(bob['id'],'book',{},dedup='one-reservation')
        workers.replies[(read,'slots')]=[{'reauth_required':True},{'reauth_required':True}]
        if failed_login:workers.replies[(read,'login')]=[{'error':'Login failed'}]
        # Even a broken worker may not turn a write into an automatic replay.
        workers.replies[(write,'book')]=[{'reauth_required':True}]
        await ticks(runner,20)
        assert store.job(alice['id'],read)['status']=='failed'
        assert store.job(bob['id'],write)['status']=='failed'
        assert sum(call['kind']=='login' for call in workers.calls)==1
        assert sum(call['job_id']==write for call in workers.calls)==1
        assert store.enqueue(bob['id'],'book',{},dedup='one-reservation')==write
    asyncio.run(scenario())


def test_login_dependency_failure_and_suspension_cancel_parked_reads(hub,monkeypatch):
    async def scenario():
        runner,store,_,(owner,alice,bob,_)=runner_for(hub);workers=Workers(monkeypatch)
        read=store.enqueue(alice['id'],'account',{});login=store.enqueue(alice['id'],'login',{})
        workers.replies[(read,'account')]=[{'reauth_required':True}]
        workers.replies[(login,'login')]=[{'error':'Login failed'}]
        await ticks(runner,15)
        assert store.job(alice['id'],read)['status']=='failed'
        other=store.enqueue(bob['id'],'slots',{})
        with store.conn() as c:c.execute("UPDATE jobs SET status='waiting_auth',auth_job_id=id,reauth_attempts=1 WHERE id=?",(other,))
        store.moderate(owner['id'],bob['id'],suspended=True)
        await ticks(runner)
        assert store.job(bob['id'],other)['status']=='cancelled'
        assert not any(call['job_id']==other for call in workers.calls)
    asyncio.run(scenario())


def test_restart_replays_only_safe_reads_preserves_waiters_and_dedup(hub,monkeypatch):
    runner,store,_,(_,alice,bob,charlie)=runner_for(hub)
    read=store.enqueue(alice['id'],'slots',{})
    write=store.enqueue(bob['id'],'book',{},dedup='unknown-write')
    parked=store.enqueue(charlie['id'],'bookings',{})
    with store.conn() as c:
        c.execute("UPDATE jobs SET status='running' WHERE id IN (?,?)",(read,write))
        c.execute("UPDATE jobs SET status='waiting_auth',auth_job_id=id,reauth_attempts=1 WHERE id=?",(parked,))
    runner.recover()
    assert store.job(alice['id'],read)['status']=='queued'
    assert store.job(bob['id'],write)['status']=='unknown'
    assert store.job(charlie['id'],parked)['status']=='waiting_auth'
    assert store.enqueue(bob['id'],'book',{},dedup='unknown-write')==write


def test_restart_never_replays_an_implicit_auth_phase_that_started(hub,monkeypatch):
    async def scenario():
        runner,store,_,(_,alice,bob,_)=runner_for(hub);workers=Workers(monkeypatch)
        interrupted=store.enqueue(alice['id'],'slots',{},dedup='interrupted-auth-read')
        unstarted=store.enqueue(bob['id'],'slots',{})
        with store.conn() as c:
            c.execute("UPDATE jobs SET status='waiting_auth',auth_job_id=id,reauth_attempts=1 WHERE id IN (?,?)",(interrupted,unstarted))
            c.execute('UPDATE jobs SET auth_started=123 WHERE id=?',(interrupted,))
        runner.recover()
        assert store.job(alice['id'],interrupted)['status']=='unknown'
        assert store.job(bob['id'],unstarted)['status']=='waiting_auth'
        await ticks(runner,20)
        assert not any(call['job_id']==interrupted for call in workers.calls)
        assert store.job(bob['id'],unstarted)['status']=='done'
        assert store.enqueue(alice['id'],'slots',{},dedup='interrupted-auth-read')==interrupted
    asyncio.run(scenario())


def test_implicit_auth_is_persistently_claimed_before_subprocess_launch(hub,monkeypatch):
    async def scenario():
        runner,store,_,(_,alice,_,_)=runner_for(hub);workers=Workers(monkeypatch)
        read=store.enqueue(alice['id'],'slots',{})
        workers.replies[(read,'slots')]=[{'reauth_required':True}]
        workers.gates[(read,'login')]=asyncio.Event()
        await ticks(runner)
        assert (read,'login') in workers.active
        with store.conn() as c:
            row=c.execute('SELECT status,auth_started FROM jobs WHERE id=?',(read,)).fetchone()
        assert row['status']=='waiting_auth' and row['auth_started']>0
        # A second runner after restart sees an uncertain native phase.
        runner.recover()
        assert store.job(alice['id'],read)['status']=='unknown'
        workers.gates[(read,'login')].set();await ticks(runner)
        assert store.job(alice['id'],read)['status']=='unknown'
    asyncio.run(scenario())


def test_community_join_locks_both_participant_and_host_tenants(hub,monkeypatch):
    async def scenario():
        runner,store,community,(_,alice,bob,charlie)=runner_for(hub);workers=Workers(monkeypatch)
        actual=booking();community.sync(alice['id'],[actual],device_verified=True)
        community.share(alice['id'],actual['id'],True)
        match=community.list(bob['id'])[0]
        join=store.enqueue(bob['id'],'community_join',{'match_id':match['id'],'participant_id':bob['id']})
        host_read=store.enqueue(alice['id'],'slots',{})
        other=store.enqueue(charlie['id'],'slots',{})
        workers.gates[(join,'community_join')]=asyncio.Event()
        await ticks(runner)
        assert store.job(alice['id'],host_read)['status']=='queued'
        assert store.job(charlie['id'],other)['status']=='done'
        assert next(call for call in workers.calls if call['job_id']==join)['user_id']==alice['id']
        workers.gates[(join,'community_join')].set();await ticks(runner)
        assert store.job(alice['id'],host_read)['status']=='done'
    asyncio.run(scenario())


def test_waiting_auth_counts_towards_global_and_profile_capacity(hub):
    _,store,_,_=runner_for(hub)
    users=[store.login_google({'sub':str(i),'email':f'person{i}@example.test','email_verified':True}) for i in range(35)]
    jobs=[store.enqueue(users[i//3]['id'],'slots',{}) for i in range(100)]
    with store.conn() as c:c.execute("UPDATE jobs SET status='waiting_auth',auth_job_id=id,reauth_attempts=1 WHERE id=?",(jobs[0],))
    with pytest.raises(ValueError):store.enqueue(users[0]['id'],'slots',{})
    with pytest.raises(ValueError):store.enqueue(users[-1]['id'],'slots',{})


@pytest.mark.parametrize('kind',['slots','book'])
def test_worker_native_auth_sentinel_is_only_returned_for_safe_reads(tmp_path,monkeypatch,kind):
    import io
    from padelbot import web_worker
    from padelbot.auth import NativeAuthRequired
    result=tmp_path/'result.json'
    monkeypatch.setenv('PADELWEB_RESULT',str(result))
    monkeypatch.setattr(web_worker.sys,'stdin',SimpleNamespace(buffer=io.BytesIO(json.dumps({'kind':kind,'payload':{}}).encode())))
    monkeypatch.setattr(web_worker.signal,'signal',lambda *args:None)
    def expired(*args):raise NativeAuthRequired('Never expose this provider payload')
    monkeypatch.setattr(web_worker,'execute',expired)
    monkeypatch.setattr(web_worker,'_reservation_started',False)
    web_worker.main()
    data=json.loads(result.read_text())
    assert 'Never expose' not in result.read_text()
    assert data.get('reauth_required') is (True if kind=='slots' else None)
    assert ('error' in data) is (kind=='book')


def test_scheduler_reuses_profile_snapshot_but_checks_opening_each_second(hub,monkeypatch):
    from datetime import datetime,timedelta,timezone
    from padelbot import web_runtime,platform_service
    runner,store,_,(_,alice,_,_)=runner_for(hub)
    store.preferences(alice['id'],{'automatic_payment':True,'max_amount':20})
    clock=[0];scans=[];enqueued=[]
    base=datetime(2030,1,1,tzinfo=timezone.utc)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return (base+timedelta(seconds=clock[0])).astimezone(tz)
    class Service:
        def __init__(self,*args):pass
        def can_autobook(self,user_id):
            scans.append((clock[0],user_id))
            return user_id==alice['id']
    class Rules:
        def list_rules(self,**kwargs):return [SimpleNamespace(id=1)]
        def has_booking_for_day(self,*args):return False
    async def sleep(seconds):
        assert seconds==1
        clock[0]+=1
        if clock[0]==17:raise asyncio.CancelledError()
    original=store.enqueue
    def enqueue(*args,**kwargs):
        enqueued.append((clock[0],args[0],args[1],args[2]))
        return original(*args,**kwargs)
    monkeypatch.setattr(web_runtime,'datetime',Clock)
    monkeypatch.setattr(web_runtime.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(web_runtime.asyncio,'sleep',sleep)
    monkeypatch.setattr(platform_service,'PlatformService',Service)
    monkeypatch.setattr(web_runtime,'rules_for',lambda *args:Rules())
    monkeypatch.setattr(web_runtime,'rule_window',lambda *args:(base+timedelta(days=7),base+timedelta(days=7,hours=1),base+timedelta(seconds=6)))
    monkeypatch.setattr(store,'enqueue',enqueue)
    with pytest.raises(asyncio.CancelledError):asyncio.run(runner.scheduler())
    assert enqueued==[(0,alice['id'],'login',{'preflight':True}),(6,alice['id'],'rule',{'rule_id':1,'target':(base+timedelta(days=7)).isoformat()})]
    assert [stamp for stamp,user_id in scans if user_id==alice['id']]==[0,15]


def test_preflight_requires_current_eligible_autobooking_and_uses_renewal_context(hub,monkeypatch):
    from padelbot.platform_service import PlatformService
    async def scenario():
        runner,store,community,(_,alice,_,_)=runner_for(hub);workers=Workers(monkeypatch)
        from padelbot.web_cards import CardVault
        key=store.root/'card-test.key';key.write_bytes(b'q'*32)
        store.card_vault_key_file=key
        vault=CardVault(store,key)
        service=PlatformService(store,community)
        for card,enabled,expected in [(False,True,'cancelled'),(True,False,'cancelled'),(True,True,'done')]:
            vault.delete(alice['id'])
            if card:vault.save(alice['id'],holder='Alice',number='4242424242424242',month=12,year=2035)
            service.remember_payment(alice['id'],{'balance':0,'direct_debit':False,'stored_card':False})
            store.preferences(alice['id'],{'automatic_payment':enabled,'payment_mode':'kul','max_amount':20})
            job=store.enqueue(alice['id'],'login',{'preflight':True})
            await ticks(runner)
            assert store.job(alice['id'],job)['status']==expected
        assert len(workers.calls)==1
        assert workers.calls[0]['context']=='renewal'
    asyncio.run(scenario())
