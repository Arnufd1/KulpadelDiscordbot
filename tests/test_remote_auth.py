import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from padelbot import android, remote_auth, remote_auth_worker, web_device
from padelbot.config import settings


UID='a'*32
LINK='https://icts.kuleuven.be/apps/authenticator/current_challenge'


def profile():
    return {'version':1,'enrollment_verified':True,'name':'padelreservering-auth','serial':'emulator-5554',
            'device_id':'stable-native-device','app_version':'2.1.16',
            'steps':[{'action':'pin','selector':{'resourceId':'pin'},'guard':{'resourceId':'guard'}}]}


def request(tmp_path,mode='approve'):
    return {'version':1,'mode':mode,'home':str(tmp_path/'platform'/'profiles'/UID/'emulators'),
            'name':'Padel_'+UID,'profile':profile(),'pin':'7392','failure_latched':False,
            'telemetry':{'user_id':UID,'job_id':'b'*32,'context':'renewal'}}


def test_native_envelope_has_no_tokens_keys_cookie_paths_or_environment(tmp_path,monkeypatch):
    item=request(tmp_path)
    monkeypatch.setattr(settings,'padelbot_android_emulator_home',Path(item['home']))
    monkeypatch.setattr(settings,'padelbot_android_emulator_name',item['name'])
    monkeypatch.setattr(settings,'padelbot_android_failure_file',tmp_path/'failure')
    monkeypatch.setenv('DISCORD_BOT_TOKEN','never transfer')
    monkeypatch.setenv('PADELWEB_TELEMETRY_DB',str(tmp_path/'metrics.sqlite3'))
    encoded=remote_auth.native_request('approve',profile=profile(),pin='7392')
    assert set(encoded)=={'version','mode','home','name','profile','pin','failure_latched','telemetry'}
    assert all(term not in json.dumps(encoded) for term in ['never transfer','master.key','tokens.enc','session.json'])
    assert encoded['home']==item['home']


@pytest.mark.parametrize('change',[
    {'home':'/etc'},{'name':'Padel_other'},{'mode':'shell'},{'command':'id'},
    {'profile':None},{'profile':{'enrollment_verified':False}},{'pin':'not a PIN'},
    {'failure_latched':True},{'telemetry':{'user_id':UID,'job_id':'url','context':'renewal'}},
])
def test_remote_endpoint_rejects_unassigned_identity_and_arbitrary_operations(tmp_path,change):
    item=request(tmp_path);item.update(change)
    with pytest.raises((ValueError,TypeError)):
        remote_auth_worker.validate_request(item,tmp_path)


def test_remote_configuration_uses_ephemeral_material_keeps_exact_android_storage(tmp_path,monkeypatch):
    # Snapshot settings because configure intentionally switches only this
    # isolated process's native paths.
    for name in type(settings).model_fields:
        monkeypatch.setattr(settings,name,getattr(settings,name))
    root=tmp_path/'data';scratch=tmp_path/'tmpfs';scratch.mkdir()
    item=request(root);home,name=remote_auth_worker.validate_request(item,root)
    monkeypatch.setenv('PADELBOT_AUTH_NATIVE_ROOT',str(root))
    remote_auth_worker.configure(item,scratch,home,name)
    assert settings.padelbot_android_emulator_home==home
    assert settings.padelbot_android_profile_file==scratch/'profile.json'
    assert settings.padelbot_auth_remote_command is None
    assert android.AndroidPinStore(settings.padelbot_key_file,settings.padelbot_android_pin_file).load()=='7392'
    assert not settings.padelbot_token_file.exists()
    assert not settings.padelbot_storage_file.exists()
    assert '7392' not in settings.padelbot_android_pin_file.read_text()


def test_enrollment_calibration_is_saved_on_app_using_existing_stable_key(tmp_path):
    key=tmp_path/'master.key';original=Fernet.generate_key();key.write_bytes(original)
    env={'PADELBOT_KEY_FILE':str(key),'PADELBOT_TOKEN_FILE':str(tmp_path/'tokens.enc'),
         'PADELBOT_ANDROID_PIN_FILE':str(tmp_path/'pin.enc'),'PADELBOT_ANDROID_PROFILE_FILE':str(tmp_path/'profile.json')}
    web_device.persist_remote_profile(env,profile(),'7392')
    web_device.persist_remote_profile(env,profile(),'7392')
    assert key.read_bytes()==original
    assert android.AndroidPinStore(key,tmp_path/'pin.enc').load()=='7392'
    assert json.loads((tmp_path/'profile.json').read_text())==profile()
    assert not (tmp_path/'tokens.enc').exists()


def test_bridge_receives_no_pin_link_or_native_configuration_in_arguments(tmp_path,monkeypatch):
    executable=tmp_path/'fixed-bridge';executable.write_text('')
    monkeypatch.setattr(settings,'padelbot_auth_remote_command',executable)
    monkeypatch.setattr(settings,'padelbot_android_emulator_home',tmp_path/'emulators')
    monkeypatch.setattr(settings,'padelbot_android_failure_file',tmp_path/'failure')
    captured={}
    class Process:
        def __init__(self,command,**kwargs):
            captured['command']=command;captured['options']=kwargs
            self.stdin=io.BytesIO()
            self.stdout=io.BytesIO(b'{"ready":true}\n{"approved":true,"attempted_pin":true}\n{"closed":true}\n')
        def poll(self):return None
        def wait(self,timeout):return 0
    monkeypatch.setattr(remote_auth.subprocess,'Popen',Process)
    provider=remote_auth.RemoteApprover(profile(),'7392')
    provider.prepare();provider(LINK)
    sent=provider.process.stdin.getvalue()
    provider.close()
    assert captured['command']==[str(executable),'approve']
    assert '7392' in sent.decode() and LINK in sent.decode()
    assert '7392' not in repr(captured) and LINK not in repr(captured)
    assert provider.attempted_pin is True


@pytest.mark.parametrize('interruption',[RuntimeError('connection lost'),SystemExit(1),KeyboardInterrupt()])
def test_lost_approval_response_conservatively_latches_pin(tmp_path,monkeypatch,interruption):
    provider=remote_auth.RemoteApprover(profile(),'7392')
    provider.device_info={'remote':True}
    provider.process=SimpleNamespace()
    monkeypatch.setattr(provider,'_send',lambda value:None)
    monkeypatch.setattr(provider,'_receive',lambda:(_ for _ in ()).throw(interruption))
    with pytest.raises(type(interruption)):provider(LINK)
    assert provider.attempted_pin
    with pytest.raises(RuntimeError,match='already been attempted'):provider(LINK)


def test_rejected_before_pin_is_not_falsely_latched(tmp_path,monkeypatch):
    provider=remote_auth.RemoteApprover(profile(),'7392')
    provider.device_info={'remote':True};provider.process=SimpleNamespace()
    monkeypatch.setattr(provider,'_send',lambda value:None)
    provider.responses.put(b'{"error":"native unavailable","attempted_pin":false}')
    with pytest.raises(RuntimeError):provider(LINK)
    assert not provider.attempted_pin


def test_android_remote_mode_never_starts_local_emulator(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'padelbot_auth_remote_command',tmp_path/'bridge')
    monkeypatch.setattr(settings,'padelbot_android_failure_file',tmp_path/'failure')
    monkeypatch.setattr(android,'read_profile',profile)
    monkeypatch.setattr(android.AndroidPinStore,'load',lambda _: '7392')
    actions=[]
    class Remote:
        attempted_pin=True;device_info={'remote':True}
        def __init__(self,*args):actions.append('init')
        def prepare(self):actions.append('prepare')
        def __call__(self,link):actions.append('approve')
        def on_failure(self):actions.append('failure')
        def close(self):actions.append('close')
    monkeypatch.setattr(remote_auth,'RemoteApprover',Remote)
    monkeypatch.setattr(android,'doctor',lambda:pytest.fail('Local Android must stay off'))
    provider=android.AndroidApprover();provider.prepare();provider(LINK);provider.close()
    assert actions==['init','prepare','approve','close']


def test_remote_input_is_bounded_and_not_arbitrary_json():
    with pytest.raises(ValueError):remote_auth_worker.read_message(io.StringIO('x'*(remote_auth.MAX_LINE+1)))
    with pytest.raises(ValueError):remote_auth_worker.read_message(io.StringIO('[]\n'))
    with pytest.raises(ValueError):remote_auth.encode_request({'pin':'x'*remote_auth.MAX_LINE})


def test_native_worker_cannot_switch_forced_ssh_operation(tmp_path,monkeypatch):
    monkeypatch.setenv('PADELBOT_AUTH_WORKER_MODE','enroll')
    with pytest.raises(ValueError,match='fixed SSH'):
        remote_auth_worker.validate_request(request(tmp_path),tmp_path)


def test_remote_stop_releases_identity_lock_even_if_transport_cleanup_fails():
    async def run():
        released=[]
        manager=web_device.DeviceManager(None)
        manager.remote_identity_lock=SimpleNamespace(release=lambda:released.append(True))
        manager.remote_env={'native':'test'};manager.user_id=UID
        async def broken():raise ValueError('malformed native stream')
        manager._stop_process=broken
        with pytest.raises(ValueError):await manager._stop()
        assert released==[True]
        assert manager.remote_identity_lock is None and manager.remote_env is None
    asyncio.run(run())


def test_remote_enrollment_returns_only_saved_status_and_persists_locally(tmp_path,monkeypatch):
    from padelbot import web_runtime
    executable=tmp_path/'bridge';executable.write_text('')
    env={'PADELBOT_AUTH_REMOTE_COMMAND':str(executable),'PADELBOT_KEY_FILE':str(tmp_path/'master.key'),
         'PADELBOT_TOKEN_FILE':str(tmp_path/'tokens.enc'),'PADELBOT_ANDROID_PIN_FILE':str(tmp_path/'pin.enc'),
         'PADELBOT_ANDROID_PROFILE_FILE':str(tmp_path/'profile.json'),
         'PADELBOT_ANDROID_EMULATOR_HOME':str(tmp_path/'emulators'),
         'PADELBOT_ANDROID_EMULATOR_NAME':'KulPadelAuth','PADELBOT_ANDROID_FAILURE_FILE':str(tmp_path/'failure')}
    monkeypatch.setattr(web_runtime,'tenant_env',lambda *args:env)
    captured=[]
    async def run():
        class Input:
            def write(self,value):captured.append(json.loads(value))
            async def drain(self):pass
        class Process:
            returncode=None;stdin=Input()
            stdout=asyncio.StreamReader()
            async def wait(self):self.returncode=0;return 0
        process=Process()
        for item in [{'ready':True},{'saved':True,'profile':profile()},{'closed':True}]:
            process.stdout.feed_data(json.dumps(item).encode()+b'\n')
        process.stdout.feed_eof()
        async def launch(*command,**kwargs):
            assert command==(str(executable),'enroll')
            return process
        monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
        manager=web_device.DeviceManager(SimpleNamespace(audit=lambda *args:None))
        assert await manager.start({'id':UID})=={'ready':True}
        assert await manager.command(UID,{'action':'finish','pin':'7392'})=={'saved':True}
        assert manager.remote_identity_lock is None
        assert manager.process is None
    asyncio.run(run())
    assert captured[0]['mode']=='enroll' and captured[0]['pin'] is None
    assert captured[1]=={'action':'finish','pin':'7392'}
    assert android.AndroidPinStore(tmp_path/'master.key',tmp_path/'pin.enc').load()=='7392'
    assert json.loads((tmp_path/'profile.json').read_text())==profile()


def test_remote_interruption_guard_is_durable_before_network_cleanup(tmp_path,monkeypatch):
    failure=tmp_path/'failure.json'
    monkeypatch.setattr(settings,'padelbot_android_failure_file',failure)
    provider=android.AndroidApprover.__new__(android.AndroidApprover)
    provider.attempted_pin=False
    def interrupted_remote_cleanup():
        assert failure.exists()
        raise SystemExit(1)
    provider._remote=SimpleNamespace(attempted_pin=True,on_failure=interrupted_remote_cleanup)
    with pytest.raises(SystemExit):provider.on_failure()
    assert provider.attempted_pin and failure.exists()
