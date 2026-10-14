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
import subprocess
import sys
import time
from urllib.parse import urlsplit


def registration_link(link: str) -> str:
    parts=urlsplit(link)
    if (parts.scheme!="https" or parts.netloc!="icts.kuleuven.be" or parts.query or parts.fragment
            or not re.fullmatch(r"/apps/authenticator/[A-Za-z0-9_-]+/?",parts.path)):
        raise ValueError("Gebruik de officiële KUL Authenticator-koppellink uit accountbeheer.")
    return link


def decode_registration_qr(encoded: str) -> str:
    import io
    from PIL import Image
    import zxingcpp
    try:
        raw=base64.b64decode(encoded,validate=True)
        if len(raw)>2_000_000:
            raise ValueError()
        with Image.open(io.BytesIO(raw)) as image:
            if image.width*image.height>4_000_000:
                raise ValueError()
            codes=zxingcpp.read_barcodes(image.convert('RGB'),formats=zxingcpp.BarcodeFormat.QRCode)
            if len(codes)!=1:
                raise ValueError()
            return registration_link(codes[0].text)
    except Exception:
        raise ValueError("Geen geldige KUL-registratie-QR-code gevonden. Upload één duidelijke QR-code uit KUL-accountbeheer.") from None


def provision():
    from .config import settings
    home = settings.padelbot_android_emulator_home.resolve()
    name = settings.padelbot_android_emulator_name
    avd = home / (name+".avd")
    if (home / (name+".ini")).exists():
        return
    if shutil.disk_usage(home.parent).free < 6*1024**3:
        raise ValueError("Er is momenteel onvoldoende opslagruimte voor een nieuw toestel. Neem contact op met de beheerder.")
    avd.mkdir(parents=True,exist_ok=True,mode=0o700)
    # Fresh userdata comes from the vendor image. No registered account is cloned.
    (home/(name+".ini")).write_text(f"avd.ini.encoding=UTF-8\npath={avd}\ntarget=android-35\n")
    config = {"AvdId":name,"avd.ini.displayname":"Sigmadvisor private authenticator","abi.type":"x86_64",
              "hw.cpu.arch":"x86_64","hw.cpu.ncore":"1","hw.ramSize":"2560","hw.lcd.width":"480",
              "hw.lcd.height":"800","hw.lcd.density":"240","hw.keyboard":"yes","hw.mainKeys":"no",
              "hw.gpu.enabled":"yes","hw.gpu.mode":"swiftshader_indirect","hw.camera.back":"none",
              "hw.camera.front":"none","hw.audioInput":"no","hw.audioOutput":"no",
              "disk.dataPartition.size":"2147483648","image.sysdir.1":"system-images/android-35/google_apis/x86_64/",
              "tag.id":"google_apis","tag.display":"Google APIs","PlayStore.enabled":"false",
              "showDeviceFrame":"no","fastboot.forceColdBoot":"yes","fastboot.forceFastBoot":"no"}
    (avd/"config.ini").write_text("".join(f"{k}={v}\n" for k,v in config.items()))


def save_profile(pin: str):
    from .android import doctor, AndroidPinStore, connect_device, PACKAGE
    from .config import settings
    from .web_worker import initialise_key
    if not re.fullmatch(r"[0-9]{4}",pin):
        raise ValueError("Gebruik de viercijferige PIN die je in dit toestel hebt ingesteld.")
    info = doctor()
    if info["app_version"] != "2.1.16":
        raise ValueError("Deze appversie moet eerst door de beheerder worden gecontroleerd.")
    device = connect_device()
    device.app_start(PACKAGE)
    if not device(packageName=PACKAGE,resourceId=PACKAGE+":id/account").wait(timeout=15):
        raise ValueError("Registreer eerst je KUL-account in dit virtuele toestel.")
    initial = initialise_key()
    profile = {"version":1,"serial":info["serial"],"device_id":info["device_id"],"app_version":info["app_version"],
               "startup_guard":{"resourceId":PACKAGE+":id/account"},"steps":[
                   {"action":"click","selector":{"resourceId":PACKAGE+":id/positive_button","text":"NEXT"},
                    "guard":{"resourceId":PACKAGE+":id/title","text":"Select account to log in:"}},
                   {"action":"pin","selector":{"resourceId":PACKAGE+":id/pin"},
                    "guard":{"resourceId":PACKAGE+":id/title","text":"Is it you trying to sign in?"},
                    "length":4,"empty_selector":{"resourceId":PACKAGE+":id/pin_input_not_set"},
                    "keypad":{str(i):{"resourceId":PACKAGE+f":id/button{i}","text":str(i)} for i in range(10)}}]}
    AndroidPinStore(initial.key_file,settings.padelbot_android_pin_file).save(pin)
    settings.padelbot_android_profile_file.write_text(json.dumps(profile),encoding="utf-8")
    # Do not clear an existing failed-PIN latch through an ordinary save.


def child_main():
    from .android import run_adb, find_adb, PACKAGE, doctor
    from .config import settings
    from .emulator import ensure_emulator
    signal.signal(signal.SIGTERM,lambda *_:sys.exit(1))
    lease = None
    try:
        provision()
        lease = ensure_emulator()
        if lease is None:
            raise ValueError("Het toestel is elders geopend; sluit die sessie eerst.")
        installed = run_adb("shell","pm","list","packages",PACKAGE,serial=settings.padelbot_android_serial)
        if f"package:{PACKAGE}" not in installed.splitlines():
            apk = Path(".tools/authenticator-apk/official-2.1.16.apk").resolve()
            if not apk.is_file():
                raise ValueError("De officiële Authenticator-installatie ontbreekt op de server.")
            run_adb("install",str(apk),serial=settings.padelbot_android_serial)
        from .android import connect_device
        device=connect_device()
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
                elif action == "screen":
                    result = subprocess.run([str(find_adb()),"-s",settings.padelbot_android_serial,"exec-out","screencap","-p"],
                                            capture_output=True,timeout=15,check=True)
                    answer = {"image":base64.b64encode(result.stdout).decode()}
                elif action == "tap":
                    x,y = int(request["x"]),int(request["y"])
                    if not (0<=x<480 and 0<=y<800):
                        raise ValueError("Ongeldige schermpositie.")
                    run_adb("shell","input","tap",str(x),str(y),serial=settings.padelbot_android_serial)
                    answer = {}
                elif action == "back":
                    run_adb("shell","input","keyevent","4",serial=settings.padelbot_android_serial)
                    answer = {}
                elif action == "link":
                    link=registration_link(str(request["link"]))
                    run_adb("shell","am","start","-a","android.intent.action.VIEW","-d",link,"-p",PACKAGE,
                            serial=settings.padelbot_android_serial)
                    answer = {}
                elif action == "finish":
                    save_profile(str(request["pin"]))
                    answer = {"saved":True}
                else:
                    raise ValueError("Onbekende actie.")
                print(json.dumps(answer),flush=True)
            except ValueError as error:
                print(json.dumps({"error":str(error)}),flush=True)
            except Exception:
                print(json.dumps({"error":"Toestelactie mislukt. Controleer het scherm."}),flush=True)
    except BaseException:
        print(json.dumps({"error":"Het virtuele toestel kon niet starten. Probeer later opnieuw."}),flush=True)
    finally:
        if lease:
            lease.close()


class DeviceManager:
    def __init__(self, store):
        self.store = store
        self.user_id = None
        self.process = None
        self.started = 0
        self.touched = 0
        self.lock = asyncio.Lock()

    async def start(self,user:dict):
        from .web_runtime import tenant_env
        async with self.lock:
            if self.process is not None and self.process.returncode is None:
                if self.user_id != user["id"]:
                    raise ValueError("Iemand koppelt momenteel een toestel. Probeer het over enkele minuten opnieuw.")
                self.touched = time.monotonic()
                return {"ready":True}
            if user["owner"]:
                raise ValueError("Jouw bestaande toestel is al gekoppeld. Het hoeft niet opnieuw geregistreerd te worden.")
            self.user_id = user["id"]
            self.started = self.touched = time.monotonic()
            self.process = await asyncio.create_subprocess_exec(sys.executable,"-m","padelbot.web_device",
                env=tenant_env(self.store,user),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,
                limit=8*1024*1024)
            try:
                result = json.loads(await asyncio.wait_for(self.process.stdout.readline(),timeout=180))
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
            if command["action"]!="screen":
                self.touched = time.monotonic()
            self.process.stdin.write(json.dumps(command).encode()+b"\n")
            await self.process.stdin.drain()
            try:
                result = json.loads(await asyncio.wait_for(self.process.stdout.readline(),timeout=45))
            except BaseException:
                await self._stop()
                raise
            if command["action"] == "finish" and result.get("saved"):
                await self._stop()
            return result

    async def _stop(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(),timeout=40)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.process = None
        self.user_id = None

    async def close(self):
        async with self.lock:
            await self._stop()

    async def reap(self):
        while True:
            await asyncio.sleep(10)
            if self.process and (time.monotonic()-self.touched>120 or time.monotonic()-self.started>600):
                await self.close()


if __name__=="__main__":
    child_main()
