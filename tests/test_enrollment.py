import asyncio
import json
from types import SimpleNamespace
import xml.etree.ElementTree as ET
import pytest
from test_web import portal,sign_in
from padelbot.android import PACKAGE
from padelbot.web_device import DeviceManager,enrollment_phase,create_enrollment_pin


def xml(title='',account=False,initial=False):
    root=ET.Element('hierarchy')
    ET.SubElement(root,'node',{'package':PACKAGE,'text':title,'resource-id':PACKAGE+':id/title'})
    if account:ET.SubElement(root,'node',{'package':PACKAGE,'resource-id':PACKAGE+':id/account'})
    if initial:ET.SubElement(root,'node',{'package':PACKAGE,'resource-id':PACKAGE+':id/initialise'})
    return ET.tostring(root,encoding='unicode')


class PinDevice:
    def __init__(self,title='Enter a new PIN code (1/2)'):
        self.title=title;self.inputs=[];self.current=[];self.bad_key=False
    def dump_hierarchy(self):return xml(self.title,account=self.title=='registered')
    def __call__(self,resourceId,text=None):
        if resourceId.endswith(':id/pin_input_not_set'):return SimpleNamespace(count=4-len(self.current))
        assert resourceId==PACKAGE+':id/button'+text
        def click():
            self.inputs.append(text);self.current.append(text)
            if len(self.current)==4:
                self.current=[]
                self.title='Confirm your new PIN (2/2)' if self.title=='Enter a new PIN code (1/2)' else 'registered'
        return SimpleNamespace(count=0 if self.bad_key else 1,exists=True,click=click)


def test_pin_setup_enters_once_in_create_and_confirm_only():
    device=PinDevice()
    assert create_enrollment_pin(device,'7392')=={'phase':'registered'}
    assert ''.join(device.inputs)=='73927392'


@pytest.mark.parametrize('title',['Is it you trying to sign in?','Select account to log in:','registered','Unrecognised screen'])
def test_pin_setup_never_approves_a_login_or_unknown_screen(title):
    device=PinDevice(title)
    with pytest.raises(ValueError) as error:create_enrollment_pin(device,'7392')
    assert not device.inputs and '7392' not in str(error.value)


def test_changed_or_partly_entered_keypad_is_not_replayed():
    device=PinDevice();device.current=['7']
    with pytest.raises(ValueError):create_enrollment_pin(device,'7392')
    assert not device.inputs
    device=PinDevice();device.bad_key=True
    with pytest.raises(ValueError):create_enrollment_pin(device,'7392')
    assert not device.inputs


def test_phases_reveal_no_account_or_pin_contents():
    assert enrollment_phase(xml('Account private@example.test',account=True))=='registered'
    assert enrollment_phase(xml(initial=True))=='needs_registration'
    assert enrollment_phase(xml('Enter a new PIN code (1/2)'))=='pin_create'
    assert enrollment_phase(xml('Confirm your new PIN (2/2)'))=='pin_confirm'
    assert enrollment_phase(xml('Is it you trying to sign in?'))=='login_qr'


def test_slow_start_returns_immediately_is_private_and_can_be_cancelled(monkeypatch):
    async def run():
        events=[];started=[]
        manager=DeviceManager(SimpleNamespace(audit=lambda user,event:events.append(event)))
        pending=asyncio.Event()
        async def start(user):
            started.append(user['id']);manager.user_id=user['id']
            await pending.wait()
            return {'ready':True}
        monkeypatch.setattr(manager,'start',start)
        assert (await manager.begin({'id':'alice'}))['state']=='starting'
        await asyncio.sleep(0)
        assert (await manager.status('alice'))['state']=='starting'
        assert await manager.status('bob')=={'state':'idle','ready':False}
        assert (await manager.begin({'id':'alice'}))['state']=='starting'
        with pytest.raises(ValueError):await manager.begin({'id':'bob'})
        assert started==['alice']
        await manager.close()
        assert manager.startup_task.cancelled() and manager.enrollment_owner is None
        assert events==['device_start_requested']
    asyncio.run(run())


def test_failed_start_has_actionable_state_without_leaking_another_user(monkeypatch):
    async def run():
        manager=DeviceManager(SimpleNamespace(audit=lambda *a:None))
        async def start(user):return {'error':'Toestel kon niet starten','stage':'boot','error_type':'RuntimeError'}
        monkeypatch.setattr(manager,'start',start)
        await manager.begin({'id':'alice'});await manager.startup_task
        assert (await manager.status('alice'))['state']=='failed'
        assert (await manager.status('bob'))['state']=='idle'
        await manager.close()
    asyncio.run(run())


def test_enrollment_http_start_is_csrf_protected_and_status_is_tenant_private(portal,monkeypatch):
    app,client,store,(_,alice,bob)=portal
    manager=app.state.devices
    async def start(user):await asyncio.Event().wait()
    monkeypatch.setattr(manager,'start',start)
    assert client.get('/api/device/status').status_code==401
    headers=sign_in(client,store,alice)
    assert client.post('/api/device/start',json={}).status_code==403
    assert client.post('/api/device/start',json={},headers=headers).json()['state']=='starting'
    assert client.get('/api/device/status').json()['state']=='starting'
    other_headers=sign_in(client,store,bob)
    assert client.get('/api/device/status').json()=={'state':'idle','ready':False}
    assert client.post('/api/device/control',json={'action':'stop'},headers=other_headers).status_code==404
    headers=sign_in(client,store,alice)
    assert client.post('/api/device/control',json={'action':'stop'},headers=headers).status_code==200
