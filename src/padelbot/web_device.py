"""Short-lived, private enrollment sessions. No ADB or emulator port is published."""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import re
import signal
import shutil
import sys
import time
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
from loguru import logger


def registration_link(link: str) -> str:
    parts=urlsplit(link)
    if (parts.scheme!="https" or parts.netloc!="icts.kuleuven.be" or parts.query or parts.fragment
            or not re.fullmatch(r"/apps/authenticator/[A-Za-z0-9_-]+/?",parts.path)):
        raise ValueError("Gebruik de officiële KUL Authenticator-koppellink uit accountbeheer.")
    return link


def decode_registration_qr(encoded: str) -> str:
    import io
    from PIL import Image,ImageOps
    import zxingcpp
    try:
        raw=base64.b64decode(encoded,validate=True)
    except Exception:
        raise ValueError('Dit is geen geldige afbeelding. Plak een screenshot of kies een PNG-, JPG- of WebP-bestand.') from None
    if len(raw)>8_000_000:raise ValueError('De afbeelding is te groot. Gebruik een screenshot van maximaal 8 MB.')
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.width*image.height>16_000_000:raise ValueError('size')
            rgb=ImageOps.exif_transpose(image).convert('RGB')
    except Exception:
        raise ValueError('De afbeelding kan niet worden gelezen. Maak een uitsnede rond de QR-code en probeer opnieuw.') from None
    # Search the full screenshot first; UI, text and unrelated QR codes may
    # surround the registration code. Overlapping crops help smaller codes.
    rgb.thumbnail((4096,4096),Image.Resampling.LANCZOS)
    seen=set();valid=set();download=False
    def scan(image):
        nonlocal download
        for code in zxingcpp.read_barcodes(ImageOps.expand(image,border=24,fill='white'),formats=zxingcpp.BarcodeFormat.QRCode):
            payload=bytes(code.bytes)
            if payload in seen:continue
            seen.add(payload)
            link=_registration_payload(payload)
            if link:valid.add(link)
            else:
                try:download|=urlsplit(payload.decode('ascii')).hostname in {'play.google.com','apps.apple.com','itunes.apple.com'}
                except (UnicodeDecodeError,ValueError):pass
        if len(valid)>1:raise ValueError('Er staan meerdere KUL-registratiecodes in de afbeelding. Kies een screenshot met één registratiecode.')
    scan(rgb)
    if not valid:
        scan(ImageOps.autocontrast(ImageOps.grayscale(rgb)))
    if not valid and max(rgb.size)>1600:
        def origins(length):return sorted({0,max(0,(length-1600)//2),max(0,length-1600)})
        for top in origins(rgb.height):
            for left in origins(rgb.width):
                scan(rgb.crop((left,top,min(left+1600,rgb.width),min(top+1600,rgb.height))))
    if valid:return next(iter(valid))
    if download:raise ValueError('Dit is de download-QR voor de app. Klik in KUL-accountbeheer op Volgende tot de registratie-QR verschijnt en voeg die afbeelding toe.')
    if not seen:raise ValueError('Geen QR-code gevonden. Zorg dat de volledige QR zichtbaar en scherp is; de rest van je screenshot mag blijven staan.')
    if len(seen)>1:raise ValueError('Er staan meerdere QR-codes in de afbeelding, maar geen KUL-registratiecode. Ga bij Toestel toevoegen verder tot de registratie-QR verschijnt.')
    raise ValueError('De QR is gelezen, maar bevat geen KUL-registratiecode. Gebruik Toestel toevoegen in KUL-accountbeheer.')


def _registration_payload(payload:bytes) -> str|None:
    # nextAuth enrolment QRs are binary. Its documented deep link is the same
    # raw bytes encoded as unpadded URL-safe base64, under the official app URL.
    # Plain URLs still go through the exact KUL host/path allowlist.
    if payload and all(32<=byte<=126 or byte in {9,10,13} for byte in payload):
        try:return registration_link(payload.decode('ascii').strip())
        except ValueError:return None
    if not 16<=len(payload)<=1024:return None
    token=base64.urlsafe_b64encode(payload).decode('ascii').rstrip('=')
    return registration_link('https://icts.kuleuven.be/apps/authenticator/'+token)


def provision():
    from .config import settings
    from .emulator import require_persisted_avd
    home = settings.padelbot_android_emulator_home.resolve()
    name = settings.padelbot_android_emulator_name
    avd = home / (name+".avd")
    require_persisted_avd(home,name)
    if (home / (name+".ini")).exists():
        config=avd/'config.ini'
        if config.is_file():
            text=config.read_text()
            lines=[line for line in text.splitlines() if not line.startswith('avd.ini.displayname=')]
            updated='\n'.join(lines+['avd.ini.displayname=padelreservering-auth'])+'\n'
            if updated!=text:
                pending=config.with_suffix('.ini.tmp')
                pending.write_text(updated);pending.chmod(0o600);pending.replace(config)
        return
    if shutil.disk_usage(home.parent).free < 6*1024**3:
        raise ValueError("Er is momenteel onvoldoende opslagruimte voor een nieuw toestel. Neem contact op met de beheerder.")
    avd.mkdir(parents=True,exist_ok=True,mode=0o700)
    # Fresh userdata comes from the vendor image. No registered account is cloned.
    (home/(name+".ini")).write_text(f"avd.ini.encoding=UTF-8\npath={avd}\ntarget=android-35\n")
    config = {"AvdId":name,"avd.ini.displayname":"padelreservering-auth","abi.type":"x86_64",
              "hw.cpu.arch":"x86_64","hw.cpu.ncore":"1","hw.ramSize":"2560","hw.lcd.width":"480",
              "hw.lcd.height":"800","hw.lcd.density":"240","hw.keyboard":"yes","hw.mainKeys":"no",
              "hw.gpu.enabled":"yes","hw.gpu.mode":"swiftshader_indirect","hw.camera.back":"none",
              "hw.camera.front":"none","hw.audioInput":"no","hw.audioOutput":"no",
              "disk.dataPartition.size":"2147483648","image.sysdir.1":"system-images/android-35/google_apis/x86_64/",
              "tag.id":"google_apis","tag.display":"Google APIs","PlayStore.enabled":"false",
              "showDeviceFrame":"no","fastboot.forceColdBoot":"yes","fastboot.forceFastBoot":"no"}
    (avd/"config.ini").write_text("".join(f"{k}={v}\n" for k,v in config.items()))


def save_profile(pin: str, *, persist: bool = True):
    from .android import doctor, AndroidPinStore, connect_device, PACKAGE
    from .config import settings
    from .web_worker import initialise_key
    if not re.fullmatch(r"[0-9]{4}",pin):
        raise ValueError("Gebruik de viercijferige PIN die je in dit toestel hebt ingesteld.")
    info = doctor()
    if info["app_version"] != "2.1.16":
        raise ValueError("Deze appversie moet eerst door de beheerder worden gecontroleerd.")
    device = connect_device()
    verify_saved_account(device)
    profile = {"version":1,"enrollment_verified":True,"name":"padelreservering-auth","serial":info["serial"],"device_id":info["device_id"],"app_version":info["app_version"],
               "startup_guard":{"resourceId":PACKAGE+":id/account"},"steps":[
                   {"action":"click","selector":{"resourceId":PACKAGE+":id/positive_button","text":"NEXT"},
                    "guard":{"resourceId":PACKAGE+":id/title","text":"Select account to log in:"}},
                   {"action":"pin","selector":{"resourceId":PACKAGE+":id/pin"},
                    "guard":{"resourceId":PACKAGE+":id/title","text":"Is it you trying to sign in?"},
                    "length":4,"empty_selector":{"resourceId":PACKAGE+":id/pin_input_not_set"},
                    "keypad":{str(i):{"resourceId":PACKAGE+f":id/button{i}","text":str(i)} for i in range(10)}}]}
    if settings.padelbot_android_emulator_autostart:
        from .emulator import AVD_IDENTITY_FILES
        avd=settings.padelbot_android_emulator_home/(settings.padelbot_android_emulator_name+'.avd')
        profile['native_storage']=[name for name in AVD_IDENTITY_FILES if (avd/name).is_file() and (avd/name).stat().st_size>0]
        if 'userdata-qemu.img' not in profile['native_storage']:
            raise ValueError('KUL kon de koppeling nog niet bewaren. Probeer opnieuw.')
    if persist:
        initial = initialise_key()
        AndroidPinStore(initial.key_file,settings.padelbot_android_pin_file).save(pin)
        from .idp import save_state
        save_state(settings.padelbot_android_profile_file,profile)
    # Do not clear an existing failed-PIN latch through an ordinary save.
    return profile


def complete_profile(pin:str,checked:bool=False,*,persist:bool=True):
    from .config import settings
    failed=settings.padelbot_android_failure_file.exists()
    if failed and checked is not True:
        raise ValueError('De PIN is geblokkeerd na een mislukte poging. Neem contact op met de beheerder.')
    profile=save_profile(pin) if persist else save_profile(pin,persist=False)
    if failed and checked is True:
        settings.padelbot_android_failure_file.unlink(missing_ok=True)
    return profile


def enrollment_phase(xml: str) -> str:
    """Return only a known stage, never account names or PIN contents."""
    from .android import PACKAGE
    nodes=list(ET.fromstring(xml).iter('node'))
    ids={n.get('resource-id','') for n in nodes if n.get('package')==PACKAGE}
    texts={n.get('text','') for n in nodes if n.get('package')==PACKAGE}
    if 'Enter a new PIN code (1/2)' in texts:return 'pin_create'
    if 'Confirm your new PIN (2/2)' in texts:return 'pin_confirm'
    if texts & {'PIN mismatch, try again …','Please pick a more hard to guess PIN.'}:return 'pin_rejected'
    if 'For additional protection, some accounts require a second authentication factor (PIN).\n\nPlease set a PIN for the app.' in texts:return 'pin_intro'
    if {'Create new account','KU Leuven'}<=texts and PACKAGE+':id/activity_confirm' in ids:return 'enroll_intro'
    if 'Create new account' in texts and {PACKAGE+':id/activity_second_factor',PACKAGE+':id/pin'}<=ids:
        if any(n.get('text','').strip() for n in nodes if n.get('resource-id')==PACKAGE+':id/pin_error'):return 'pin_rejected'
        return 'enroll_pin'
    if texts & {'Is it you trying to sign in?','Select account to log in:'}:return 'login_qr'
    # Enrollment/login activities also contain an account label. Only the
    # home screen's saved account proves that enrollment has completed.
    if {PACKAGE+':id/activity_main',PACKAGE+':id/account'}<=ids:return 'registered'
    if PACKAGE+':id/initialise' in ids:return 'needs_registration'
    return 'waiting'


def verify_saved_account(device):
    """Require a saved home-screen account, including after an app restart."""
    from .android import PACKAGE,run_adb
    from .config import settings
    def wait_registered():
        deadline=time.monotonic()+20
        while enrollment_phase(device.dump_hierarchy())!='registered':
            if time.monotonic()>=deadline:
                raise ValueError('KUL heeft het account nog niet op dit toestel bewaard. Voeg opnieuw een registratie-QR toe.')
            time.sleep(.3)
    wait_registered()
    # Let the Activity/SDK finish committing before proving process persistence.
    device.press('home')
    time.sleep(1)
    run_adb('shell','sync',serial=settings.padelbot_android_serial)
    device.app_stop(PACKAGE)
    device.app_start(PACKAGE,wait=True)
    wait_registered()
    # The proof and the persisted Android data must refer to the same restart.
    run_adb('shell','sync',serial=settings.padelbot_android_serial)


def prepare_enrollment(device):
    """Advance only the known new-PIN introduction, never a sign-in request."""
    from .android import PACKAGE
    phase=enrollment_phase(device.dump_hierarchy())
    if phase in {'enroll_intro','pin_intro'}:
        button=device(resourceId=PACKAGE+':id/positive_button',text='NEXT')
        if button.count==1 and button.exists:
            button.click()
            phase=enrollment_phase(device.dump_hierarchy())
    return phase


def create_enrollment_pin(device,pin):
    """One guarded create/confirm attempt. Never enter a PIN on a login screen."""
    from .android import PACKAGE
    if not re.fullmatch(r'[0-9]{4}',pin):raise ValueError('Kies een PIN van vier cijfers.')
    if enrollment_phase(device.dump_hierarchy())!='pin_create':
        raise ValueError('KUL vraagt nog niet om een nieuwe PIN. Er is geen PIN ingevoerd.')
    for phase in ['pin_create','pin_confirm']:
        deadline=time.monotonic()+12
        while enrollment_phase(device.dump_hierarchy())!=phase:
            current=enrollment_phase(device.dump_hierarchy())
            if current=='pin_rejected':raise ValueError('KUL weigert deze PIN. Kies een minder voorspelbare PIN.')
            if current not in {'waiting','pin_create','pin_confirm'} or time.monotonic()>=deadline:
                raise ValueError('De PIN-stap is veranderd. Controleer de voortgang voordat je opnieuw probeert.')
            time.sleep(.2)
        if device(resourceId=PACKAGE+':id/pin_input_not_set').count!=4:
            raise ValueError('De PIN-invoer is niet leeg. Er is geen PIN ingevoerd.')
        for digit in pin:
            if enrollment_phase(device.dump_hierarchy())!=phase:
                raise ValueError('Het PIN-scherm is veranderd. Er is niet verder ingevoerd.')
            button=device(resourceId=PACKAGE+':id/button'+digit,text=digit)
            if button.count!=1 or not button.exists:raise ValueError('Het PIN-toetsenbord is veranderd.')
            button.click()
    deadline=time.monotonic()+30
    while True:
        phase=enrollment_phase(device.dump_hierarchy())
        if phase=='registered':return {'phase':phase}
        if phase=='pin_rejected':raise ValueError('KUL weigert deze PIN. Kies een minder voorspelbare PIN.')
        if time.monotonic()>=deadline:raise ValueError('KUL heeft de registratie nog niet bevestigd. Controleer de voortgang; de PIN wordt niet opnieuw ingevoerd.')
        time.sleep(.3)


def submit_enrollment_pin(device,pin):
    """One explicit PIN submission on the account-enrolment screen only."""
    from .android import PACKAGE
    if not re.fullmatch(r'[0-9]{4}',pin):raise ValueError('Gebruik vier cijfers.')
    phase=enrollment_phase(device.dump_hierarchy())
    if phase not in {'enroll_pin','pin_confirm'}:raise ValueError('Het toestel vraagt nu niet om een registratie-PIN.')
    if device(resourceId=PACKAGE+':id/pin_input_not_set').count!=4:raise ValueError('De PIN-invoer is niet leeg. Controleer het toestel.')
    for digit in pin:
        if enrollment_phase(device.dump_hierarchy())!=phase:raise ValueError('Het PIN-scherm is veranderd. Er is niet verder ingevoerd.')
        button=device(resourceId=PACKAGE+':id/button'+digit,text=digit)
        if button.count!=1 or not button.exists:raise ValueError('Het PIN-toetsenbord is veranderd.')
        button.click()
    started=time.monotonic()
    while True:
        current=enrollment_phase(device.dump_hierarchy())
        if current=='registered':return {'phase':current}
        if current=='pin_rejected':raise ValueError('De PIN is niet aanvaard. Controleer het toestel voordat je opnieuw probeert.')
        if current in {'enroll_pin','pin_confirm'} and time.monotonic()-started>=1 and device(resourceId=PACKAGE+':id/pin_input_not_set').count==4:
            return {'phase':current,'confirm':True}
        if time.monotonic()-started>20:raise ValueError('De registratie is nog niet bevestigd. Controleer het toestel; de PIN wordt niet opnieuw ingevoerd.')
        time.sleep(.2)


def setup_enrollment_pin(device,pin,confirm_pin):
    """One user-confirmed create/confirm operation; never replay a login PIN."""
    if not re.fullmatch(r'[0-9]{4}',pin) or pin!=confirm_pin:
        raise ValueError('Vul tweemaal dezelfde PIN van vier cijfers in.')
    phase=enrollment_phase(device.dump_hierarchy())
    if phase=='pin_create':return create_enrollment_pin(device,pin)
    result=submit_enrollment_pin(device,pin)
    if result.get('confirm') and phase=='enroll_pin':
        result=submit_enrollment_pin(device,confirm_pin)
    if result.get('confirm'):
        raise ValueError('KUL heeft de PIN nog niet bevestigd. De PIN wordt niet opnieuw ingevoerd.')
    return result


def child_main(*, export_profile:bool=False):
    from filelock import FileLock
    from .android import run_adb, PACKAGE, doctor
    from .config import settings
    from .emulator import ensure_emulator
    signal.signal(signal.SIGTERM,lambda *_:sys.exit(1))
    lease = None
    stage='provision'
    identity_lock=FileLock(str(settings.padelbot_token_file)+'.renew.lock',timeout=600)
    try:
        # Match auth renewal's lock order: identity first, then the shared emulator.
        # A PIN changed interactively must be saved before another approver reads it.
        identity_lock.acquire()
        provision()
        stage='boot'
        lease = ensure_emulator()
        if lease is None:
            raise ValueError("Het toestel is elders geopend; sluit die sessie eerst.")
        installed = run_adb("shell","pm","list","packages",PACKAGE,serial=settings.padelbot_android_serial)
        stage='install'
        if f"package:{PACKAGE}" not in installed.splitlines():
            apk = Path(".tools/authenticator-apk/official-2.1.16.apk").resolve()
            if not apk.is_file():
                raise ValueError("De officiële Authenticator-installatie ontbreekt op de server.")
            run_adb("install",str(apk),serial=settings.padelbot_android_serial)
        from .android import connect_device
        stage='verify_app'
        if doctor()['app_version']!='2.1.16':raise ValueError('Deze Authenticator-versie wordt nog niet ondersteund.')
        device=connect_device()
        run_adb('shell','settings','put','global','device_name','padelreservering-auth',serial=settings.padelbot_android_serial)
        stage='open_app'
        device.app_start(PACKAGE,wait=True)
        # First launch initialises nextAuth. Avoid presenting a black transition screen.
        device(packageName=PACKAGE).wait(timeout=30)
        print(json.dumps({"ready":True}),flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            action = request["action"]
            try:
                if action == "stop":
                    print("{}",flush=True)
                    break
                elif action == 'status':
                    answer={'phase':prepare_enrollment(device)}
                elif action == 'setup_pin':
                    answer=setup_enrollment_pin(device,str(request['pin']),str(request.get('confirm_pin','')))
                elif action == "link":
                    link=registration_link(str(request["link"]))
                    run_adb("shell","am","start","-a","android.intent.action.VIEW","-d",link,"-p",PACKAGE,
                            serial=settings.padelbot_android_serial)
                    answer = {}
                elif action == "finish":
                    profile=complete_profile(str(request["pin"]),persist=not export_profile)
                    answer = {"saved":True}
                    if export_profile:answer['profile']=profile
                else:
                    raise ValueError("Onbekende actie.")
                print(json.dumps(answer),flush=True)
            except ValueError as error:
                print(json.dumps({"error":str(error)}),flush=True)
            except Exception:
                print(json.dumps({"error":"Koppelen mislukt. Probeer opnieuw of neem contact op met de beheerder."}),flush=True)
    except BaseException as error:
        print(json.dumps({"error":"Het virtuele toestel kon niet starten. Probeer opnieuw of meld deze fout.",
                          'stage':stage,'error_type':type(error).__name__}),flush=True)
    finally:
        try:
            if lease:
                lease.close()
        finally:
            identity_lock.release()


def persist_remote_profile(env:dict, profile:dict, pin:str):
    """Keep long-lived encryption keys and PIN storage on the application VM."""
    from filelock import FileLock
    from .android import AndroidPinStore
    from .idp import save_state
    from .store import TokenStore
    if (not isinstance(profile,dict) or profile.get('version')!=1
            or profile.get('enrollment_verified') is not True or profile.get('app_version')!='2.1.16'
            or profile.get('name')!='padelreservering-auth' or not re.fullmatch(r'[0-9]{4}',pin)):
        raise ValueError('Invalid native enrollment result')
    if set(profile)-{'version','enrollment_verified','name','serial','device_id','app_version','startup_guard','steps','native_storage'}:
        raise ValueError('Unexpected native enrollment fields')
    store=TokenStore(Path(env['PADELBOT_KEY_FILE']),Path(env['PADELBOT_TOKEN_FILE']))
    store.key_file.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with FileLock(str(store.key_file)+'.init.lock',timeout=15):
        if not store.key_file.exists():store.init_key()
    AndroidPinStore(store.key_file,Path(env['PADELBOT_ANDROID_PIN_FILE'])).save(pin)
    save_state(Path(env['PADELBOT_ANDROID_PROFILE_FILE']),profile)


class DeviceManager:
    def __init__(self, store):
        self.store = store
        self.user_id = None
        self.process = None
        self.started = 0
        self.touched = 0
        self.lock = asyncio.Lock()
        self.lifecycle_lock=asyncio.Lock()
        self.startup_task=None
        self.startup_result={}
        self.enrollment_owner=None
        self.remote_identity_lock=None
        self.remote_env=None

    async def begin(self,user):
        async with self.lifecycle_lock:
            return await self._begin(user)

    async def _begin(self,user):
        # This endpoint must return before Cloudflare's request timeout on a cold boot.
        if self.startup_task and not self.startup_task.done():
            if self.enrollment_owner!=user['id']:raise ValueError('Iemand koppelt momenteel een toestel. Probeer het later opnieuw.')
            return {'state':'starting','ready':False}
        if self.process and self.process.returncode is None:
            if self.user_id!=user['id']:raise ValueError('Iemand koppelt momenteel een toestel. Probeer het later opnieuw.')
            return {'state':'ready','ready':True}
        self.enrollment_owner=user['id'];self.startup_result={}
        self.store.audit(user['id'],'device_start_requested')
        async def launch():
            try:self.startup_result=await self.start(user)
            except asyncio.CancelledError:raise
            except Exception:self.startup_result={'error':'Het toestel kon niet starten. Probeer opnieuw.'}
            self.store.audit(user['id'],'device_start_failed' if self.startup_result.get('error') else 'device_ready')
            if self.startup_result.get('error'):
                logger.warning('Enrollment start failed profile={} stage={} type={}',user['id'],self.startup_result.get('stage','startup'),self.startup_result.get('error_type','unknown'))
            else:logger.info('Enrollment device ready profile={}',user['id'])
        self.startup_task=asyncio.create_task(launch())
        return {'state':'starting','ready':False}

    async def status(self,user_id):
        if self.enrollment_owner!=user_id:return {'state':'idle','ready':False}
        self.touched=time.monotonic()
        if self.startup_task and not self.startup_task.done():return {'state':'starting','ready':False}
        if self.startup_result.get('error'):return {'state':'failed','ready':False,**self.startup_result}
        if not self.process or self.process.returncode is not None:return {'state':'idle','ready':False}
        return {'state':'ready','ready':True,**await self.command(user_id,{'action':'status'})}

    async def start(self,user:dict):
        from .web_runtime import tenant_env
        from .remote_auth import bridge_command, native_request, encode_request
        async with self.lock:
            if self.process is not None and self.process.returncode is None:
                if self.user_id != user["id"]:
                    raise ValueError("Iemand koppelt momenteel een toestel. Probeer het over enkele minuten opnieuw.")
                self.touched = time.monotonic()
                return {"ready":True}
            self.user_id = user["id"]
            self.started = self.touched = time.monotonic()
            env=tenant_env(self.store,user)
            command=bridge_command('enroll',env)
            try:
                if command:
                    from filelock import FileLock
                    # Hold the application's identity lock throughout enrollment,
                    # just like a local worker: renewal must not read a partial PIN.
                    self.remote_identity_lock=FileLock(env['PADELBOT_TOKEN_FILE']+'.renew.lock',timeout=1)
                    self.remote_identity_lock.acquire()
                    self.remote_env=env
                self.process = await asyncio.create_subprocess_exec(*(command or [sys.executable,"-m","padelbot.web_device"]),
                    env=env,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,
                    limit=64*1024 if command else 8*1024*1024)
                if command:
                    self.process.stdin.write(encode_request(native_request('enroll',env=env)))
                    await self.process.stdin.drain()
                result = json.loads(await asyncio.wait_for(self.process.stdout.readline(),timeout=360 if command else 180))
                if command:
                    from .remote_auth import ingest_response
                    ingest_response(result,env)
                if "error" in result:
                    await self._stop()
                return result
            except BaseException:
                await self._stop()
                raise

    async def command(self,user_id: str,command: dict):
        async with self.lock:
            if user_id!=self.user_id or not self.process or self.process.returncode is not None:
                raise ValueError("Start eerst je eigen toestel.")
            self.touched = time.monotonic()
            self.process.stdin.write(json.dumps(command).encode()+b"\n")
            await self.process.stdin.drain()
            try:
                result = json.loads(await asyncio.wait_for(self.process.stdout.readline(),timeout=75 if command['action']=='setup_pin' else 45))
                if self.remote_env:
                    from .remote_auth import ingest_response
                    ingest_response(result,self.remote_env)
            except BaseException:
                await self._stop()
                raise
            if command["action"] == "finish" and result.get("saved"):
                if self.remote_env:
                    try:
                        persist_remote_profile(self.remote_env,result.pop('profile',None),str(command['pin']))
                    except Exception:
                        await self._stop()
                        raise ValueError('De koppeling kon niet veilig worden bewaard. Probeer opnieuw.') from None
                self.store.audit(user_id,'device_profile_saved')
                logger.info('Enrollment profile saved profile={}',user_id)
                await self._stop()
            elif command['action'] in {'link','setup_pin'}:
                self.store.audit(user_id,'device_'+command['action']+('_failed' if result.get('error') else '_sent'))
                logger.info('Enrollment action={} failed={} profile={}',command['action'],bool(result.get('error')),user_id)
            return result

    async def _stop(self):
        try:
            await self._stop_process()
        finally:
            self.process = None
            self.user_id = None
            if self.remote_identity_lock is not None:
                self.remote_identity_lock.release()
                self.remote_identity_lock=None
            self.remote_env=None

    async def _stop_process(self):
        if self.process and self.process.returncode is None:
            # Let the worker leave its command loop and flush/close Android in
            # its finally block. SIGTERM remains a fallback for an unresponsive
            # command, rather than interrupting a completed profile save.
            try:
                if self.process.stdin:
                    self.process.stdin.write(b'{"action":"stop"}\n')
                    await self.process.stdin.drain()
                await asyncio.wait_for(self.process.wait(),timeout=40)
                if self.remote_env and self.process.stdout:
                    # A final event is sent after Android flushes and stops.
                    # It is telemetry only; never expose it to the website.
                    from .remote_auth import ingest_response
                    for _ in range(3):
                        line=await asyncio.wait_for(self.process.stdout.readline(),timeout=2)
                        if not line:break
                        try:ingest_response(json.loads(line),self.remote_env)
                        except (ValueError,TypeError):pass
            except (asyncio.TimeoutError,BrokenPipeError,ConnectionResetError):
                if self.process.returncode is None:
                    self.process.terminate()
                    try:
                        await asyncio.wait_for(self.process.wait(),timeout=40)
                    except asyncio.TimeoutError:
                        self.process.kill()
                        await self.process.wait()

    async def close(self):
        async with self.lifecycle_lock:
            await self._close()

    async def _close(self):
        if self.startup_task and not self.startup_task.done():
            self.startup_task.cancel()
            await asyncio.gather(self.startup_task,return_exceptions=True)
        async with self.lock:
            await self._stop()
            self.enrollment_owner=None

    async def reap(self):
        while True:
            await asyncio.sleep(10)
            if self.process and (time.monotonic()-self.touched>120 or time.monotonic()-self.started>600):
                await self.close()


if __name__=="__main__":
    child_main()
