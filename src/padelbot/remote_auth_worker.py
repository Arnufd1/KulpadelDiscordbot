"""Forced-command SSH endpoint for native authentication only.

The application VM retains all HTTP sessions and long-lived encryption keys.
This worker receives one native profile and PIN over private stdin, places the
temporary calibration material in tmpfs, and keeps only Android userdata.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
import time

from .config import settings
from .remote_auth import MAX_LINE

_telemetry_started=time.time()
_real_stdout=None


def read_message(stream) -> dict:
    line = stream.readline(MAX_LINE + 1)
    if not line or len(line) > MAX_LINE:
        raise ValueError("Invalid native request")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("Invalid native request")
    return value


def validate_request(request: dict, root: Path) -> tuple[Path, str]:
    if set(request) - {"version", "mode", "home", "name", "profile", "pin", "failure_latched", "telemetry"}:
        raise ValueError("Unsupported native request")
    if request.get("version") != 1 or request.get("mode") not in {"approve", "enroll"}:
        raise ValueError("Unsupported native protocol")
    assigned_mode=os.environ.get('PADELBOT_AUTH_WORKER_MODE')
    if assigned_mode and assigned_mode!=request['mode']:
        raise ValueError('Native request does not match its fixed SSH command')
    root = root.resolve()
    home = Path(request["home"])
    if not home.is_absolute():
        raise ValueError("Native storage must be absolute")
    home = home.resolve()
    relative = home.relative_to(root).as_posix()
    if relative != "emulators" and not re.fullmatch(r"platform/profiles/[a-f0-9]{32}/emulators", relative):
        raise ValueError("Native storage is outside its assigned profile")
    name = request.get("name", "")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,80}", name):
        raise ValueError("Invalid native identity")
    if relative == "emulators" and name != "KulPadelAuth":
        raise ValueError("Invalid legacy native identity")
    if relative != "emulators" and name != "Padel_" + relative.split("/")[2]:
        raise ValueError("Native identity does not match profile")
    if request.get("failure_latched"):
        raise ValueError("A previous PIN attempt needs explicit recovery")
    profile = request.get("profile")
    if profile is not None and (not isinstance(profile, dict) or profile.get("enrollment_verified") is not True):
        raise ValueError("Native profile was not verified")
    if request["mode"] == "approve" and profile is None:
        raise ValueError("Native profile is missing")
    pin = request.get("pin")
    if pin is not None and (not isinstance(pin, str) or not re.fullmatch(r"[0-9]{4,12}", pin)):
        raise ValueError("Invalid PIN format")
    telemetry=request.get('telemetry',{})
    if (not isinstance(telemetry,dict) or set(telemetry)-{'user_id','job_id','context'}
            or not re.fullmatch(r'[a-f0-9]{32}',telemetry.get('job_id',''))
            or telemetry.get('context') not in {'login','renewal','enrollment','probe'}
            or telemetry.get('user_id','') and not re.fullmatch(r'[a-f0-9]{32}',telemetry['user_id'])):
        raise ValueError('Invalid telemetry context')
    return home, name


def configure(request: dict, scratch: Path, home: Path, name: str):
    # Never trust filesystem/tool settings sent by the application. These are
    # fixed by the remote service. Scratch is local tmpfs and not shared.
    settings.padelbot_auth_remote_command = None
    settings.padelbot_android_emulator_home = home
    settings.padelbot_android_emulator_name = name
    settings.padelbot_android_emulator_autostart = True
    settings.padelbot_android_emulator_memory_mb = 2560
    settings.padelbot_android_emulator_cores = 1
    settings.padelbot_android_serial = "emulator-5554"
    settings.padelbot_android_device_id = ""
    settings.padelbot_android_profile_file = scratch / "profile.json"
    settings.padelbot_android_pin_file = scratch / "pin.enc"
    settings.padelbot_android_failure_file = scratch / "failure.json"
    settings.padelbot_key_file = scratch / "ephemeral.key"
    settings.padelbot_token_file = scratch / "unused-token.enc"
    settings.padelbot_storage_file = scratch / "unused-session.json"
    context=request.get('telemetry',{})
    os.environ.setdefault('PADELWEB_TELEMETRY_DB',str(Path(os.environ.get('PADELBOT_AUTH_NATIVE_ROOT','/opt/padelbot/data'))/'auth-telemetry.sqlite3'))
    for suffix,key in [('USER_ID','user_id'),('JOB_ID','job_id'),('AUTH_CONTEXT','context')]:
        os.environ['PADELWEB_'+suffix]=context.get(key,'')
    if request.get("profile") is not None:
        from .idp import save_state
        save_state(settings.padelbot_android_profile_file, request["profile"])
    if request.get("pin") is not None:
        from cryptography.fernet import Fernet
        from .android import AndroidPinStore
        settings.padelbot_key_file.write_bytes(Fernet.generate_key())
        settings.padelbot_key_file.chmod(0o600)
        AndroidPinStore(settings.padelbot_key_file, settings.padelbot_android_pin_file).save(request["pin"])


def emit(value: dict):
    from .telemetry import export_events
    value['telemetry']=export_events(job_id=os.environ.get('PADELWEB_JOB_ID'),since=_telemetry_started,limit=100)
    target=_real_stdout or sys.stdout
    print(json.dumps(value, separators=(",", ":")), file=target,flush=True)


class EnrollmentOutput:
    """Add bounded native telemetry to the existing enrollment JSON stream."""
    def __init__(self):self.buffer=''
    def write(self,value):
        self.buffer+=value
        while '\n' in self.buffer:
            line,self.buffer=self.buffer.split('\n',1)
            if line:emit(json.loads(line))
        return len(value)
    def flush(self):
        if _real_stdout:_real_stdout.flush()


def run_approval(stream):
    from .android import AndroidApprover
    approver = AndroidApprover()
    try:
        approver.prepare()
        # No account labels, device IDs, secrets, provider output or screenshots.
        emit({"ready": True})
        while True:
            request = read_message(stream)
            action = request.get("action")
            if action in {"close", "stop"}:
                return
            try:
                if action == "approve" and set(request) == {"action", "link"}:
                    approver(request["link"])
                    emit({"approved": True, "attempted_pin": approver.attempted_pin})
                elif action == "failure" and set(request) == {"action"}:
                    approver.on_failure()
                    emit({"attempted_pin": approver.attempted_pin})
                else:
                    raise ValueError("Unsupported native operation")
            except Exception:
                emit({"error": "Native authentication failed", "attempted_pin": approver.attempted_pin})
    finally:
        approver.close()


def main():
    global _real_stdout
    _real_stdout=sys.stdout
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    # Never load application .env, tokens, Discord or OAuth material here. The
    # dedicated service sets PADELBOT_ENV_FILE="" and fixed native tool paths.
    try:
        request = read_message(sys.stdin)
        root = Path(os.environ.get("PADELBOT_AUTH_NATIVE_ROOT", "/opt/padelbot/data"))
        home, name = validate_request(request, root)
        runtime = Path(os.environ.get("PADELBOT_AUTH_RUNTIME_ROOT", "/run/padelbot"))
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="native-", dir=runtime) as directory:
            configure(request, Path(directory), home, name)
            request.pop("pin", None)
            if request["mode"] == "enroll":
                from .web_device import child_main
                sys.stdout=EnrollmentOutput()
                try:child_main(export_profile=True)
                finally:sys.stdout=_real_stdout
            else:
                run_approval(sys.stdin)
            # Export the stop event only after native userdata flush/shutdown.
            emit({'closed':True})
    except BaseException:
        emit({"error": "Native authentication unavailable"})


if __name__ == "__main__":
    main()
