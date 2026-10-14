"""Start an explicitly configured local virtual Authenticator when needed."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

from .config import settings


_start_lock = threading.Lock()


def ensure_emulator(timeout: float = 120) -> None:
    if not settings.padelbot_android_emulator_autostart:
        return
    from .android import list_devices, run_adb

    serial = settings.padelbot_android_serial
    match = re.fullmatch(r"emulator-(\d{4,5})", serial)
    if not match or not 5554 <= int(match[1]) <= 5682 or int(match[1]) % 2:
        raise RuntimeError("Virtual Android requires an explicit emulator serial with an even local port.")
    name = settings.padelbot_android_emulator_name
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise RuntimeError("Invalid virtual Android name.")

    with _start_lock:
        connected = next((d for d in list_devices() if d["serial"] == serial), None)
        process = None
        if connected is None:
            sdk = Path(".tools/android").resolve()
            executable = sdk / "emulator" / ("emulator.exe" if os.name == "nt" else "emulator")
            if not executable.is_file():
                found = shutil.which("emulator")
                if not found:
                    raise RuntimeError("Android Emulator is not installed; follow the software-authenticator setup.")
                executable = Path(found).resolve()
                sdk = executable.parent.parent
            avd_home = settings.padelbot_android_emulator_home.resolve()
            if not (avd_home / f"{name}.ini").is_file():
                raise RuntimeError("The configured virtual Android has not been created.")
            environment = {**os.environ, "ANDROID_SDK_ROOT": str(sdk), "ANDROID_AVD_HOME": str(avd_home)}
            command = [str(executable), "-avd", name, "-port", match[1],
                       "-no-window", "-no-audio", "-no-boot-anim", "-no-snapshot", "-gpu", "swiftshader_indirect"]
            with (avd_home / "autostart.log").open("ab") as output:
                process = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
            (avd_home / "emulator.pid").write_text(str(process.pid), encoding="ascii")
        elif connected["state"] == "unauthorized":
            raise RuntimeError("The configured virtual Android is not authorized for debugging.")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process is not None and process.poll() is not None:
                raise RuntimeError("Virtual Android stopped before startup completed; check its local log.")
            try:
                if run_adb("shell", "getprop", "sys.boot_completed", serial=serial).strip() == "1":
                    return
            except RuntimeError:
                pass
            time.sleep(1)
        raise RuntimeError("Virtual Android did not finish starting; check its local log before retrying.")
