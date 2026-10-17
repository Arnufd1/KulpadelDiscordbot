"""Private stdio transport to the on-demand native authenticator VM.

The bridge is a fixed administrator-installed executable, never a shell
command. Credentials and challenge URLs are only written to its stdin.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import threading
import uuid

from .config import settings
from loguru import logger

MAX_LINE = 64 * 1024
REMOTE_TIMEOUT = 360
ERROR = "KUL-authenticatie is tijdelijk niet beschikbaar. Probeer later opnieuw."
ERROR_STAGES = frozenset({'startup_account','calibrated_guard','foreground','pin_controls','identity','challenge_open','action','native',
                         'boot_timeout','boot_exit','boot_identity','boot_config','boot_missing','adb_unauthorized','adb_offline','adb_timeout','adb_command','native_storage'})
ERROR_TYPES = frozenset({'RuntimeError','ValueError','TimeoutError','unknown'})


def bridge_command(mode: str, env: dict[str, str] | None = None) -> list[str] | None:
    if mode not in {"approve", "enroll"}:
        raise ValueError("Invalid native operation")
    raw = env.get("PADELBOT_AUTH_REMOTE_COMMAND", "") if env is not None else settings.padelbot_auth_remote_command
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute() or not path.is_file():
        raise RuntimeError("The private authenticator bridge is unavailable.")
    return [str(path), mode]


def native_request(mode: str, *, env: dict[str, str] | None = None, profile=None, pin=None) -> dict:
    """Only native calibration and the selected userdata path cross the bridge."""
    def value(key):
        return env[key.upper()] if env is not None else getattr(settings, key)
    profile_path = Path(value("padelbot_android_profile_file"))
    if profile is None and profile_path.is_file():
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    context_env=os.environ if env is None else env
    from .telemetry import current_context
    job_id=context_env.get('PADELWEB_JOB_ID') or uuid.uuid4().hex
    return {"version": 1, "mode": mode,
            "home": str(Path(value("padelbot_android_emulator_home")).resolve()),
            "name": str(value("padelbot_android_emulator_name")),
            "profile": profile, "pin": pin,
            "failure_latched": Path(value("padelbot_android_failure_file")).exists(),
            "telemetry": {"user_id":context_env.get('PADELWEB_USER_ID',''),"job_id":job_id,
                          "context":'enrollment' if mode=='enroll' else current_context() if env is None else context_env.get('PADELWEB_AUTH_CONTEXT','login')}}


def encode_request(value: dict) -> bytes:
    encoded = json.dumps(value, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(encoded) > MAX_LINE:
        raise ValueError("Native request is too large")
    return encoded


def ingest_response(response:dict,env:dict|None=None):
    events=response.pop('telemetry',None)
    if events:
        from .telemetry import ingest_events
        source=os.environ if env is None else env
        path=source.get('PADELWEB_TELEMETRY_DB')
        if path:
            ingest_events(Path(path),events)


class RemoteApprover:
    """One native attempt. HTTP, tokens and cookies stay on the application VM."""
    def __init__(self, profile: dict, pin: str | None):
        self.profile, self.pin = profile, pin
        self.process = None
        self.responses = Queue()
        self.attempted_pin = False
        self.used = False
        self.device_info = None
        self._pin_pending = False

    def _read_output(self):
        try:
            while True:
                line = self.process.stdout.readline(MAX_LINE + 1)
                if not line:
                    self.responses.put(None)
                    return
                if len(line) > MAX_LINE:
                    self.responses.put(None)
                    return
                self.responses.put(line)
        except (OSError, ValueError):
            self.responses.put(None)

    def _receive(self, timeout=REMOTE_TIMEOUT):
        try:
            line = self.responses.get(timeout=timeout)
            if line is None:
                raise RuntimeError(ERROR)
            response = json.loads(line)
            if not isinstance(response, dict):
                raise ValueError()
        except (Empty, ValueError, TypeError):
            raise RuntimeError(ERROR) from None
        ingest_response(response)
        if isinstance(response.get('attempted_pin'),bool):
            self._pin_pending=False
        self.attempted_pin |= response.get("attempted_pin") is True
        # Worker errors deliberately contain no provider output or user data.
        if response.get("error"):
            stage=response.get('stage')
            error_type=response.get('error_type')
            if stage in ERROR_STAGES and error_type in ERROR_TYPES:
                logger.warning('Native authenticator failed stage={} type={}',stage,error_type)
            raise RuntimeError(ERROR)
        return response

    def _send(self, value):
        try:
            self.process.stdin.write(encode_request(value))
            self.process.stdin.flush()
        except (OSError, BrokenPipeError):
            raise RuntimeError(ERROR) from None

    def prepare(self):
        if self.device_info is not None:
            return
        if self.process is not None:
            raise RuntimeError(ERROR)
        command = bridge_command("approve")
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, bufsize=0)
        threading.Thread(target=self._read_output, daemon=True).start()
        self._send(native_request("approve", profile=self.profile, pin=self.pin))
        response = self._receive()
        if response.get("ready") is not True:
            raise RuntimeError(ERROR)
        self.device_info = response.get("device") or {"remote": True}

    def __call__(self, link: str):
        from .android import validate_challenge_link
        validate_challenge_link(link)
        if self.used:
            raise RuntimeError("This Android login has already been attempted; no approval retry was made.")
        self.used = True
        self.prepare()
        # If transport disappears after submission the native outcome is
        # uncertain. Conservatively retain the PIN latch until explicit review.
        self._pin_pending=self.pin is not None
        try:
            self._send({"action": "approve", "link": link})
            if self._receive().get("approved") is not True:
                raise RuntimeError(ERROR)
        finally:
            self.attempted_pin |= self._pin_pending

    def on_failure(self):
        # The parent persists its own failed-PIN latch. Never retry remotely.
        if self.process and self.process.poll() is None:
            try:
                self._send({"action": "failure"})
                self._receive(timeout=10)
            except RuntimeError:
                pass

    def close(self):
        if self.process is None:
            return
        try:
            if self.process.poll() is None:
                try:
                    self._send({"action": "close"})
                    self._receive(timeout=45)
                    self.process.wait(timeout=45)
                except (RuntimeError, subprocess.TimeoutExpired):
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=10)
        finally:
            for stream in (self.process.stdin, self.process.stdout):
                if stream:
                    stream.close()
            self.process = None
            self.pin = None
