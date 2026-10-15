"""Start the virtual Authenticator on demand and release it after login."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

from loguru import logger
from filelock import FileLock

from .config import settings


_start_lock = threading.RLock()


def _release_file_cache(roots: tuple[Path, ...]) -> None:
    """Let a Linux guest return unused Android file pages to its hypervisor."""
    if not hasattr(os, "posix_fadvise"):
        return
    for root in roots:
        for path in root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                with path.open("rb") as stream:
                    os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            except OSError:
                # This is advisory: a busy, removed or unreadable file must not
                # turn a completed login into a failure or alter any disk data.
                continue


@dataclass
class StartedEmulator:
    """Own only the emulator process launched for this login, never a PID file."""
    serial: str
    process: subprocess.Popen
    cache_roots: tuple[Path, ...] = ()
    _closed: bool = field(default=False, init=False, repr=False)
    lease: object | None = field(default=None, repr=False)

    def close(self) -> None:
        from .android import run_adb

        with _start_lock:
            if self._closed:
                return
            if self.process.poll() is None:
                try:
                    run_adb('shell', 'sync', serial=self.serial)
                except RuntimeError:
                    logger.warning('Virtual Android could not flush storage before shutdown.')
                try:
                    run_adb("emu", "kill", serial=self.serial)
                except RuntimeError:
                    logger.warning("Virtual Android did not accept shutdown; stopping its owned process.")
                try:
                    self.process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=5)
            _release_file_cache(self.cache_roots)
            self._closed = True
            if self.lease is not None:
                self.lease.release()
            logger.info("Virtual Android stopped; its CPU and memory are released.")


def ensure_emulator(timeout: float = 120) -> StartedEmulator | None:
    if not settings.padelbot_android_emulator_autostart:
        return None
    settings.padelbot_emulator_lock_file.parent.mkdir(parents=True, exist_ok=True)
    lease = FileLock(settings.padelbot_emulator_lock_file, timeout=600)
    lease.acquire()
    try:
        started = _ensure_emulator(timeout)
        if started is None:
            lease.release()
        else:
            started.lease = lease
        return started
    except BaseException:
        lease.release()
        raise


def _ensure_emulator(timeout: float = 120) -> StartedEmulator | None:
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
        started = None
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
            if settings.padelbot_android_emulator_memory_mb is not None:
                command += ["-memory", str(settings.padelbot_android_emulator_memory_mb)]
            if settings.padelbot_android_emulator_cores is not None:
                command += ["-cores", str(settings.padelbot_android_emulator_cores)]
            with (avd_home / "autostart.log").open("ab") as output:
                process = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
            started = StartedEmulator(serial, process, (avd_home / f"{name}.avd", sdk))
        elif connected["state"] == "unauthorized":
            raise RuntimeError("The configured virtual Android is not authorized for debugging.")
        elif connected["state"] == "device":
            active_name = run_adb("emu", "avd", "name", serial=serial).splitlines()[0].strip()
            if active_name != name:
                raise RuntimeError("Another user's virtual Android is active; wait for its session to finish.")

        try:
            if started is not None:
                (settings.padelbot_android_emulator_home / "emulator.pid").write_text(str(started.process.pid), encoding="ascii")
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if started is not None and started.process.poll() is not None:
                    raise RuntimeError("Virtual Android stopped before startup completed; check its local log.")
                try:
                    if run_adb("shell", "getprop", "sys.boot_completed", serial=serial).strip() == "1":
                        return started
                except RuntimeError:
                    pass
                time.sleep(1)
            raise RuntimeError("Virtual Android did not finish starting; check its local log before retrying.")
        except BaseException:
            if started is not None:
                try:
                    started.close()
                except Exception as error:
                    logger.error("Virtual Android startup cleanup failed: {}", type(error).__name__)
            raise
