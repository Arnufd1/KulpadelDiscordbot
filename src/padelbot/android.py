"""Local Android transport and diagnostics for the owner's Authenticator app."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from loguru import logger

from .config import settings
from .idp import save_state


PACKAGE = "be.kuleuven.icts.authenticator"
SAMSUNG_BIOMETRIC_PACKAGE = "com.samsung.android.biometrics.app.setting"
_CHALLENGE_PATH = re.compile(r"/apps/authenticator/[A-Za-z0-9_-]+/?\Z")


def validate_challenge_link(link: str) -> str:
    parts = urlsplit(link)
    if (parts.scheme != "https" or parts.netloc != "icts.kuleuven.be"
            or not _CHALLENGE_PATH.fullmatch(parts.path) or parts.query or parts.fragment):
        raise RuntimeError("Expected a KU Leuven Authenticator link for the current login.")
    return link


def find_adb() -> Path:
    candidates = [settings.padelbot_android_adb,
                  Path(".tools/android/platform-tools/adb.exe"),
                  Path(".tools/android/platform-tools/adb"),
                  shutil.which("adb")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    raise RuntimeError("ADB is missing. On Windows run scripts/setup-android.ps1 first.")


def run_adb(*args: str, serial: str | None = None) -> str:
    command = [str(find_adb())]
    if serial:
        command += ["-s", serial]
    command += list(args)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("ADB did not respond; check the USB connection.") from error
    if result.returncode:
        # Android may echo a complete challenge URL or PIN in an error. Do not log it.
        raise RuntimeError(f"ADB command failed (exit {result.returncode}); check the device connection.")
    return result.stdout


def list_devices() -> list[dict[str, str]]:
    lines = run_adb("devices", "-l").splitlines()
    devices = []
    for line in lines:
        if not line.strip() or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            devices.append({"serial": parts[0], "state": parts[1],
                            "model": next((p.split(":", 1)[1] for p in parts[2:] if p.startswith("model:")), "")})
    return devices


def selected_device() -> dict[str, str]:
    devices = list_devices()
    if settings.padelbot_android_serial:
        devices = [d for d in devices if d["serial"] == settings.padelbot_android_serial]
    if settings.padelbot_android_device_id:
        # Wi-Fi ports and mDNS transport names can change. Bind to the handset,
        # and collapse multiple authorized transports for that same handset.
        devices = [d for d in devices if d["state"] == "device"
                   and run_adb("shell", "getprop", "ro.serialno", serial=d["serial"]).strip()
                   == settings.padelbot_android_device_id]
        devices = devices[:1]
    if not devices:
        raise RuntimeError("No matching Android connected. Check USB authorization or Wi-Fi debugging and pairing.")
    if len(devices) != 1:
        raise RuntimeError("Several devices connected. Set PADELBOT_ANDROID_SERIAL to choose your Android.")
    device = devices[0]
    if device["state"] == "unauthorized":
        raise RuntimeError("Unlock the Android and accept the USB debugging authorization for this PC.")
    if device["state"] != "device":
        raise RuntimeError("The Android is offline; reconnect the cable and unlock the device.")
    return device


def doctor() -> dict:
    device = selected_device()
    serial = device["serial"]
    installed = run_adb("shell", "pm", "list", "packages", PACKAGE, serial=serial)
    if f"package:{PACKAGE}" not in installed.splitlines():
        raise RuntimeError("KU Leuven Authenticator is not installed on this Android.")
    package_info = run_adb("shell", "dumpsys", "package", PACKAGE, serial=serial)
    version = re.search(r"versionName=([^\s]+)", package_info)
    return {**device, "android_version": run_adb("shell", "getprop", "ro.build.version.release", serial=serial).strip(),
            "device_id": run_adb("shell", "getprop", "ro.serialno", serial=serial).strip(),
            "package": PACKAGE, "app_version": version[1] if version else "unknown"}


def connect_device(serial: str | None = None):
    device = selected_device()
    if serial is not None and device["serial"] != serial:
        raise RuntimeError("The selected Android changed; reconnect the calibrated device.")
    os.environ["ADBUTILS_ADB_PATH"] = str(find_adb())
    try:
        import uiautomator2 as u2
    except ImportError as error:
        raise RuntimeError('Install Android support: pip install -e ".[android]"') from error
    try:
        return u2.connect(device["serial"])
    except Exception as error:
        raise RuntimeError("Could not start Android UI inspection. Unlock the phone and check USB authorization.") from error


def redact_text(value: str) -> str:
    value = re.sub(r"https?://\S+", "[link]", value)
    value = re.sub(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", "[email]", value)
    value = re.sub(r"\b[ruq]\d{5,}\b", "[account]", value, flags=re.I)
    return re.sub(r"\b\d{4,}\b", "[number]", value)


def describe_hierarchy(xml: str) -> list[dict]:
    nodes = []
    for node in ET.fromstring(xml).iter("node"):
        if node.get("package") != PACKAGE:
            continue
        editable = node.get("password") == "true" or "EditText" in node.get("class", "")
        nodes.append({
            "resource_id": node.get("resource-id", ""), "class": node.get("class", ""),
            "text": "[hidden]" if editable else redact_text(node.get("text", "")),
            "description": "[hidden]" if editable else redact_text(node.get("content-desc", "")),
            "clickable": node.get("clickable") == "true", "enabled": node.get("enabled") == "true",
            "bounds": node.get("bounds", ""),
        })
    return nodes


def inspect_app(output: Path, *, open_app: bool = False) -> dict:
    device = connect_device()
    if open_app:
        device.screen_on()
        device.app_start(PACKAGE)
        device.app_wait(PACKAGE, front=True, timeout=5)
    foreground = device.app_current()
    if foreground.get("package") != PACKAGE:
        raise RuntimeError("Open KU Leuven Authenticator on the Android, then run android inspect again.")
    report = {"package": PACKAGE, "activity": foreground.get("activity", ""),
              "nodes": describe_hierarchy(device.dump_hierarchy())}
    if not report["nodes"]:
        raise RuntimeError("No visible Authenticator controls. Unlock the phone and open the app, then inspect again.")
    save_state(output, report)
    return report


def open_challenge(link: str, *, serial: str | None = None) -> None:
    validate_challenge_link(link)
    current_serial = selected_device()["serial"]
    if serial is not None and current_serial != serial:
        raise RuntimeError("The selected Android changed; login was stopped.")
    serial = current_serial
    output = run_adb("shell", "am", "start", "-W", "-a", "android.intent.action.VIEW",
                     "-d", link, "-p", PACKAGE, serial=serial)
    if "Error:" in output or "unable to resolve" in output.lower():
        raise RuntimeError("The Authenticator app did not accept the current login link.")


def start_mirror() -> int:
    device = selected_device()
    executable = shutil.which("scrcpy")
    local = Path(".tools/scrcpy/scrcpy-win64-v5.0.1/scrcpy.exe")
    if local.is_file():
        executable = str(local.resolve())
    if not executable:
        raise RuntimeError("scrcpy is missing. Run scripts/setup-mirror.ps1 on Windows.")
    environment = {**os.environ, "ADB": str(find_adb())}
    log = Path("data/android/mirror.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            [executable, "--serial", device["serial"], "--no-audio", "--stay-awake", "--max-size=1280", "--window-title=KUL-Android"],
            env=environment, stdout=output, stderr=subprocess.STDOUT,
        )
    return process.pid


_SELECTOR_KEYS = {"resourceId", "text", "description", "className"}


def _validate_selector(selector: dict) -> None:
    if not isinstance(selector, dict) or not selector or not set(selector) <= _SELECTOR_KEYS:
        raise RuntimeError("Android profile must use an explicit resource/text selector.")
    if any(not isinstance(value, str) or not value for value in selector.values()):
        raise RuntimeError("Android profile selector values must be non-empty strings.")


def read_profile() -> dict:
    path = settings.padelbot_android_profile_file
    if not path.exists():
        raise RuntimeError("Android UI profile has not been calibrated. Run android inspect first.")
    profile = json.loads(path.read_text(encoding="utf-8"))
    if (profile.get("version") != 1 or not (profile.get("device_id") or profile.get("serial"))
            or not profile.get("app_version")):
        raise RuntimeError("Android profile must specify version 1, the device ID or transport serial, and app version.")
    steps = profile.get("steps", [])
    if "startup_guard" in profile:
        _validate_selector(profile["startup_guard"])
    if not isinstance(steps, list) or not steps or len(steps) > 10:
        raise RuntimeError("Android profile must contain 1–10 calibrated steps.")
    pin_steps = 0
    for step in steps:
        if not isinstance(step, dict) or step.get("action") not in {"click", "pin"}:
            raise RuntimeError("Unsupported Android profile action.")
        pin_steps += step["action"] == "pin"
        _validate_selector(step.get("selector", {}))
        _validate_selector(step.get("guard", {}))
        if "keypad" in step:
            keypad = step["keypad"]
            if (step["action"] != "pin" or not isinstance(keypad, dict)
                    or set(keypad) != set("0123456789")):
                raise RuntimeError("A PIN keypad must map all ten observed digits.")
            for digit, selector in keypad.items():
                _validate_selector(selector)
                if selector.get("text") != digit:
                    raise RuntimeError("PIN keypad selectors must match their visible digit labels.")
            _validate_selector(step.get("empty_selector", {}))
            if type(step.get("length")) is not int or not 4 <= step["length"] <= 12:
                raise RuntimeError("PIN keypad must specify its observed PIN length.")
    if pin_steps > 1:
        raise RuntimeError("Android profile may contain only one app PIN attempt.")
    return profile


class AndroidApprover:
    """One login's link and a locally calibrated sequence, with no PIN retries."""
    def __init__(self, *, manual: bool = False):
        self.manual = manual
        self.attempted_pin = False
        self.used = False
        self.device_info = None
        self._emulator = None
        if not manual and settings.padelbot_android_failure_file.exists():
            raise RuntimeError("An earlier Android PIN attempt failed. Check the app, then run android reset-failure.")
        self.profile = None if manual else read_profile()
        self.pin = None
        if self.profile is not None:
            if any(step["action"] == "pin" for step in self.profile["steps"]):
                self.pin = AndroidPinStore(settings.padelbot_key_file, settings.padelbot_android_pin_file).load()
                if self.pin is None:
                    raise RuntimeError("No app PIN configured. Run android set-pin in your local terminal.")
                for step in self.profile["steps"]:
                    if step["action"] == "pin" and step.get("length") and len(self.pin) != step["length"]:
                        raise RuntimeError("Stored app PIN length differs from the calibrated keypad. Run android set-pin again.")

    def prepare(self) -> None:
        """Boot Android only when the IdP actually asks for authentication."""
        if self.device_info is not None:
            return
        from .emulator import ensure_emulator
        self._emulator = ensure_emulator()
        info = doctor()
        if self.profile is not None:
            identity_key = "device_id" if self.profile.get("device_id") else "serial"
            if (self.profile[identity_key] != info.get(identity_key)
                    or self.profile["app_version"] != info["app_version"]):
                raise RuntimeError("Android device or app version changed. Recalibrate before automatic approval.")
        self.device_info = info

    def close(self) -> None:
        """Release a login-owned emulator; leave manually started devices alone."""
        if self._emulator is not None:
            self._emulator.close()
            self._emulator = None

    def _enter_keypad(self, device, step: dict) -> None:
        empty = device(packageName=PACKAGE, **step["empty_selector"])
        keys = {digit: device(packageName=PACKAGE, **selector) for digit, selector in step["keypad"].items()}
        if empty.count != len(self.pin) or any(key.count != 1 for key in keys.values()):
            raise RuntimeError("PIN keypad is not empty or its controls changed; no PIN was entered.")
        self.attempted_pin = True
        for entered, digit in enumerate(self.pin):
            # Never append to someone else's partial entry, or continue on a
            # different screen. The final digit may submit automatically.
            if (device.app_current().get("package") != PACKAGE
                    or device(packageName=PACKAGE, **step["guard"]).count != 1
                    or empty.count != len(self.pin) - entered):
                raise RuntimeError("PIN screen changed during entry; the attempt was stopped.")
            keys[digit].click()

    def _select_pin_fallback(self, device) -> None:
        """Use Samsung's observed app-PIN option for this KUL login prompt."""
        scope = SAMSUNG_BIOMETRIC_PACKAGE
        button = device(packageName=scope, resourceId=scope + ":id/button_negative", text="Use PIN")
        if not button.wait(timeout=5):
            return
        current = device.app_current()
        title = device(packageName=scope, resourceId=scope + ":id/title", text="Log in")
        subject = device(packageName=scope, resourceId=scope + ":id/subtitle", text="to: KU Leuven")
        if (current.get("package") != PACKAGE
                or current.get("activity") != "com.nextauth.authenticator.authentication.SecondFactorActivity"
                or button.count != 1 or title.count != 1 or subject.count != 1):
            raise RuntimeError("Biometric prompt does not match this KUL login; no PIN fallback selected.")
        button.click()
        logger.info("Android: selected the Authenticator's normal Use PIN option.")

    def __call__(self, link: str) -> None:
        import time
        validate_challenge_link(link)
        if self.used:
            raise RuntimeError("This Android login has already been attempted; no approval retry was made.")
        self.used = True
        self.prepare()
        device = connect_device(self.device_info["serial"])
        # Repeated deep links can leave old confirmation activities underneath
        # the current one. Start this challenge with only its own app UI alive.
        device.app_stop(PACKAGE)
        device.screen_on()
        if self.profile is not None and "startup_guard" in self.profile:
            # On a freshly booted virtual Android, initialize the SDK and
            # wait for its registered account before delivering the link.
            device.app_start(PACKAGE)
            ready = device(packageName=PACKAGE, **self.profile["startup_guard"])
            if (not ready.wait(timeout=20) or ready.count != 1
                    or device.app_current().get("package") != PACKAGE):
                raise RuntimeError("Authenticator did not finish loading its registered account; no login link opened.")
        open_challenge(link, serial=self.device_info["serial"])
        logger.info("Android: opened the bot's current login challenge.")
        time.sleep(0.5)
        if self.manual:
            inspect_app(Path("data/android/login-screen.json"))
            return
        for step in self.profile["steps"]:
            if step["action"] == "pin":
                self._select_pin_fallback(device)
            guard = device(packageName=PACKAGE, **step["guard"])
            if not guard.wait(timeout=15):
                if step.get("optional") is True:
                    logger.info("Android: optional PIN screen did not appear (activity: {}).", device.app_current().get("activity", "unknown"))
                    continue
                raise RuntimeError("Authenticator screen differs from the calibrated profile; no action taken.")
            # A fresh SDK process may briefly return to the Android launcher
            # while it opens its confirmation activity. Wait for the scoped
            # guard first, then verify the foreground before any interaction.
            if device.app_current().get("package") != PACKAGE:
                raise RuntimeError("Authenticator is not in the foreground. Unlock the Android before login.")
            target = device(packageName=PACKAGE, **step["selector"])
            guard_count, target_count = guard.count, target.count
            if guard_count != 1 or target_count != 1:
                raise RuntimeError(f"Authenticator controls changed or are ambiguous (guard={guard_count}, target={target_count}); no action taken.")
            try:
                if step["action"] == "pin":
                    if "keypad" in step:
                        self._enter_keypad(device, step)
                    else:
                        self.attempted_pin = True
                        target.set_text(self.pin)
                    logger.info("Android: entered the stored app PIN once.")
                else:
                    target.click()
                    logger.info("Android: clicked the calibrated login control.")
            except Exception:
                raise RuntimeError("Android UI action failed; no PIN or approval retry was attempted.") from None
            time.sleep(0.3)

    def on_failure(self) -> None:
        if self.attempted_pin:
            save_state(settings.padelbot_android_failure_file, {"reason": "Login did not complete after an app PIN attempt."})


class AndroidPinStore:
    """A local encrypted PIN; never accepted from Discord or command-line flags."""
    def __init__(self, key_file: Path, blob_file: Path):
        self.key_file, self.blob_file = key_file, blob_file

    def save(self, pin: str) -> None:
        if not re.fullmatch(r"[0-9]{4,12}", pin):
            raise ValueError("Enter the Authenticator app's numeric PIN (4–12 digits).")
        ciphertext = Fernet(self.key_file.read_bytes()).encrypt(pin.encode())
        # Reuse the atomic writer; the ciphertext remains an opaque string.
        save_state(self.blob_file, {"encrypted_pin": ciphertext.decode()})

    def load(self) -> str | None:
        if not self.blob_file.exists():
            return None
        data = json.loads(self.blob_file.read_text(encoding="utf-8"))
        return Fernet(self.key_file.read_bytes()).decrypt(data["encrypted_pin"].encode()).decode()
