import asyncio
import base64
import io
import json
from types import SimpleNamespace
import xml.etree.ElementTree as ET
import pytest
from test_web import portal,sign_in
from padelbot.android import PACKAGE
from padelbot.web_device import DeviceManager,enrollment_phase,create_enrollment_pin,prepare_enrollment,submit_enrollment_pin


def qr_image(payload):
    from PIL import Image
    import zxingcpp
    bitmap=memoryview(zxingcpp.write_barcode(zxingcpp.BarcodeFormat.QRCode,payload,300,300))
    return Image.frombytes('L',(bitmap.shape[1],bitmap.shape[0]),bitmap.tobytes())


def encoded_image(image):
    stream=io.BytesIO();image.save(stream,format='PNG')
    return base64.b64encode(stream.getvalue()).decode('ascii')


def test_binary_nextauth_qr_preserves_raw_bytes_in_official_deep_link():
    from padelbot.web_device import decode_registration_qr
    payload=bytes(range(256))
    link=decode_registration_qr(encoded_image(qr_image(payload)))
    assert link=='https://icts.kuleuven.be/apps/authenticator/'+base64.urlsafe_b64encode(payload).decode().rstrip('=')
    token=link.rsplit('/',1)[1]
    assert base64.urlsafe_b64decode(token+'='*(-len(token)%4))==payload


def test_qr_errors_distinguish_unreadable_missing_multiple_and_wrong_codes():
    from PIL import Image
    from padelbot.web_device import decode_registration_qr
    with pytest.raises(ValueError,match='geldige afbeelding'):decode_registration_qr('not base64')
    with pytest.raises(ValueError,match='kan niet worden gelezen'):decode_registration_qr(base64.b64encode(b'not an image').decode())
    with pytest.raises(ValueError,match='Geen QR-code gevonden'):decode_registration_qr(encoded_image(Image.new('RGB',(320,320),'white')))
    wrong='https://example.test/not-kul'
    with pytest.raises(ValueError,match='QR is gelezen') as error:decode_registration_qr(encoded_image(qr_image(wrong)))
    assert wrong not in str(error.value)
    first=qr_image('https://example.test/one');second=qr_image('https://example.test/two')
    image=Image.new('RGB',(first.width+second.width+60,max(first.height,second.height)+40),'white')
    image.paste(first,(20,20));image.paste(second,(first.width+40,20))
    with pytest.raises(ValueError,match='meerdere QR-codes'):decode_registration_qr(encoded_image(image))
    with pytest.raises(ValueError,match='te groot'):decode_registration_qr(base64.b64encode(b'X'*8_000_001).decode())


@pytest.mark.parametrize('rotation',[0,90])
def test_full_phone_screenshot_finds_kul_among_text_and_unrelated_qrs(rotation):
    from PIL import Image,ImageDraw
    from padelbot.web_device import decode_registration_qr
    image=Image.new('RGB',(1440,3200),'#e4e8eb');draw=ImageDraw.Draw(image)
    for top in range(60,3100,180):
        draw.rounded_rectangle((30,top,1410,top+140),radius=20,fill='white')
        draw.text((70,top+20),'KU Leuven account - registered devices - add device',fill='black')
    image.paste(qr_image('https://play.google.com/store/apps/details?id=example.app'),(70,400))
    image.paste(qr_image('https://example.test/unrelated'),(900,1000))
    payload=bytes(range(128));image.paste(qr_image(payload),(850,2500))
    expected='https://icts.kuleuven.be/apps/authenticator/'+base64.urlsafe_b64encode(payload).decode().rstrip('=')
    assert decode_registration_qr(encoded_image(image.rotate(rotation,expand=True)))==expected


def test_download_qr_has_actionable_feedback_and_no_payload_echo():
    from padelbot.web_device import decode_registration_qr
    for link in ['https://play.google.com/store/apps/details?id=example.app','https://apps.apple.com/app/id123456789']:
        with pytest.raises(ValueError,match='download-QR') as error:decode_registration_qr(encoded_image(qr_image(link)))
        assert 'Volgende' in str(error.value) and link not in str(error.value)


def test_distinct_kul_qrs_are_ambiguous_but_duplicate_same_qr_is_safe():
    from PIL import Image
    from padelbot.web_device import decode_registration_qr
    first=qr_image('https://icts.kuleuven.be/apps/authenticator/TestCodeOne')
    second=qr_image('https://icts.kuleuven.be/apps/authenticator/TestCodeTwo')
    image=Image.new('RGB',(first.width+second.width+60,max(first.height,second.height)+40),'white')
    image.paste(first,(20,20));image.paste(second,(first.width+40,20))
    with pytest.raises(ValueError,match='meerdere KUL-registratiecodes'):decode_registration_qr(encoded_image(image))
    image.paste(first,(first.width+40,20))
    assert decode_registration_qr(encoded_image(image))=='https://icts.kuleuven.be/apps/authenticator/TestCodeOne'


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


class EnrollPinDevice(PinDevice):
    def __init__(self,rounds=1):super().__init__('Create new account');self.rounds=rounds;self.rejected=False
    def dump_hierarchy(self):
        if self.title=='registered':return xml(account=True)
        root=ET.fromstring(xml(self.title))
        for name in ['activity_second_factor','pin']:
            ET.SubElement(root,'node',{'package':PACKAGE,'resource-id':PACKAGE+':id/'+name})
        if self.rejected:ET.SubElement(root,'node',{'package':PACKAGE,'resource-id':PACKAGE+':id/pin_error','text':'Wrong PIN'})
        return ET.tostring(root,encoding='unicode')
    def __call__(self,resourceId,text=None):
        if resourceId.endswith(':id/pin_input_not_set'):return SimpleNamespace(count=4-len(self.current))
        assert resourceId==PACKAGE+':id/button'+text
        def click():
            self.inputs.append(text);self.current.append(text)
            if len(self.current)==4:
                self.current=[]
                if len(self.inputs)==4*self.rounds:self.title='registered'
        return SimpleNamespace(count=1,exists=True,click=click)


def test_observed_kul_account_confirmation_is_distinct_from_login():
    root=ET.fromstring(xml('Create new account'))
    ET.SubElement(root,'node',{'package':PACKAGE,'text':'KU Leuven'})
    ET.SubElement(root,'node',{'package':PACKAGE,'resource-id':PACKAGE+':id/activity_confirm'})
    assert enrollment_phase(ET.tostring(root,encoding='unicode'))=='enroll_intro'
    root[1].set('text','Another provider')
    assert enrollment_phase(ET.tostring(root,encoding='unicode'))=='waiting'
    assert enrollment_phase(EnrollPinDevice().dump_hierarchy())=='enroll_pin'


def test_enrollment_pin_enters_once_and_waits_for_explicit_confirmation():
    device=EnrollPinDevice(rounds=2)
    assert submit_enrollment_pin(device,'7392')=={'phase':'enroll_pin','confirm':True}
    assert ''.join(device.inputs)=='7392'
    assert submit_enrollment_pin(device,'7392')=={'phase':'registered'}
    assert ''.join(device.inputs)=='73927392'


def test_enrollment_pin_never_retries_rejected_or_partly_entered_pin():
    device=EnrollPinDevice();device.rejected=True
    with pytest.raises(ValueError):submit_enrollment_pin(device,'7392')
    assert not device.inputs
    device=EnrollPinDevice();device.current=['7']
    with pytest.raises(ValueError):submit_enrollment_pin(device,'7392')
    assert not device.inputs
    for title in ['Is it you trying to sign in?','Select account to log in:','Unrecognised screen']:
        device=PinDevice(title)
        with pytest.raises(ValueError):submit_enrollment_pin(device,'7392')
        assert not device.inputs


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
