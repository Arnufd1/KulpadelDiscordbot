from pathlib import Path
import os
import subprocess
from types import SimpleNamespace

import pytest

from padelbot import android, auth, emulator
from padelbot.config import settings


@pytest.fixture
def virtual_setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, 'padelbot_android_emulator_autostart', True)
    monkeypatch.setattr(settings, 'padelbot_android_serial', 'emulator-5554')
    monkeypatch.setattr(settings, 'padelbot_android_emulator_name', 'KulPadelAuth')
    monkeypatch.setattr(settings, 'padelbot_android_emulator_home', tmp_path / 'avds')
    monkeypatch.setattr(settings, 'padelbot_android_emulator_memory_mb', None)
    monkeypatch.setattr(settings, 'padelbot_android_emulator_cores', None)
    executable = tmp_path / '.tools/android/emulator' / ('emulator.exe' if emulator.os.name == 'nt' else 'emulator')
    executable.parent.mkdir(parents=True)
    executable.touch()
    settings.padelbot_android_emulator_home.mkdir()
    (settings.padelbot_android_emulator_home / 'KulPadelAuth.ini').write_text('path=registered-avd')
    monkeypatch.setattr(android, 'list_devices', lambda: [])
    monkeypatch.setattr(android, 'run_adb', lambda *args, **kwargs: 'KulPadelAuth\nOK' if args == ('emu', 'avd', 'name') else '1')
    return executable


def test_autostart_uses_headless_registered_avd_and_explicit_transport(virtual_setup, monkeypatch):
    starts = []
    def launch(command, **kwargs):
        starts.append((command, kwargs))
        return SimpleNamespace(pid=123, poll=lambda: None)
    monkeypatch.setattr(emulator.subprocess, 'Popen', launch)
    requested = []
    def probe(*args, serial):
        requested.append(serial)
        return '1'
    monkeypatch.setattr(android, 'run_adb', probe)
    assert isinstance(emulator.ensure_emulator(), emulator.StartedEmulator)
    assert len(starts) == 1
    command, options = starts[0]
    assert command[0] == str(virtual_setup)
    assert '-no-window' in command and '-no-audio' in command
    assert command[command.index('-avd') + 1] == 'KulPadelAuth'
    assert command[command.index('-port') + 1] == '5554'
    assert options['env']['ANDROID_AVD_HOME'] == str(settings.padelbot_android_emulator_home)
    assert requested == ['emulator-5554']


def test_existing_virtual_device_is_reused_without_launching_or_resetting(virtual_setup, monkeypatch):
    monkeypatch.setattr(android, 'list_devices', lambda: [
        {'serial': 'physical-phone', 'state': 'device'},
        {'serial': 'emulator-5554', 'state': 'device'},
    ])
    monkeypatch.setattr(emulator.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('Duplicate emulator launch'))
    assert emulator.ensure_emulator() is None


def test_other_tenants_emulator_is_never_borrowed(virtual_setup,monkeypatch):
    monkeypatch.setattr(android,'list_devices',lambda:[{'serial':'emulator-5554','state':'device'}])
    monkeypatch.setattr(android,'run_adb',lambda *a,**kw:'OtherTenant\nOK')
    with pytest.raises(RuntimeError,match="Another user's"):
        emulator.ensure_emulator()


@pytest.mark.parametrize('serial', ['physical-phone', '192.168.0.10:5554', 'emulator-5555', 'emulator-99999'])
def test_invalid_or_physical_target_is_rejected_before_device_actions(virtual_setup, monkeypatch, serial):
    monkeypatch.setattr(settings, 'padelbot_android_serial', serial)
    monkeypatch.setattr(android, 'list_devices', lambda: pytest.fail('Wrong target queried'))
    with pytest.raises(RuntimeError, match='explicit emulator serial'):
        emulator.ensure_emulator()


def test_offline_device_timeout_does_not_launch_another_copy(virtual_setup, monkeypatch):
    monkeypatch.setattr(android, 'list_devices', lambda: [{'serial': 'emulator-5554', 'state': 'offline'}])
    monkeypatch.setattr(emulator.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('Duplicate emulator launch'))
    def offline(*args, **kwargs): raise RuntimeError('Offline')
    monkeypatch.setattr(android, 'run_adb', offline)
    clock = iter([0, 0, 2])
    monkeypatch.setattr(emulator.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(emulator.time, 'sleep', lambda seconds: None)
    with pytest.raises(RuntimeError, match='did not finish'):
        emulator.ensure_emulator(timeout=1)


def test_disabled_autostart_does_not_touch_android(monkeypatch):
    monkeypatch.setattr(settings, 'padelbot_android_emulator_autostart', False)
    monkeypatch.setattr(android, 'list_devices', lambda: pytest.fail('Disabled feature queried Android'))
    emulator.ensure_emulator()


class Process:
    pid = 123

    def __init__(self, *, stubborn=False):
        self.returncode = None
        self.stubborn = stubborn
        self.actions = []

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.actions.append(('wait', timeout))
        if self.returncode is None:
            raise subprocess.TimeoutExpired('emulator', timeout)
        return self.returncode

    def terminate(self):
        self.actions.append('terminate')
        if not self.stubborn:
            self.returncode = 0

    def kill(self):
        self.actions.append('kill')
        self.returncode = 0


def test_small_host_can_limit_the_temporary_emulator(virtual_setup, monkeypatch):
    monkeypatch.setattr(settings, 'padelbot_android_emulator_memory_mb', 1536)
    monkeypatch.setattr(settings, 'padelbot_android_emulator_cores', 1)
    commands = []
    process = Process()
    monkeypatch.setattr(emulator.subprocess, 'Popen', lambda command, **kwargs: commands.append(command) or process)
    started = emulator.ensure_emulator()
    assert started.process is process
    assert commands[0][commands[0].index('-memory') + 1] == '1536'
    assert commands[0][commands[0].index('-cores') + 1] == '1'


@pytest.mark.parametrize('mode', ['graceful', 'terminate', 'kill'])
def test_shutdown_releases_only_the_owned_process_even_when_adb_fails(monkeypatch, mode):
    process = Process(stubborn=mode == 'kill')
    commands = []
    def adb(*args, serial):
        commands.append((args, serial))
        if mode != 'graceful':
            raise RuntimeError('ADB unavailable')
        if args == ('emu', 'kill'):process.returncode = 0
    monkeypatch.setattr(android, 'run_adb', adb)
    started = emulator.StartedEmulator('emulator-5554', process)
    started.close()
    assert commands == [(('shell', 'sync'), 'emulator-5554'), (('emu', 'kill'), 'emulator-5554')]
    assert process.returncode == 0
    assert ('terminate' in process.actions) == (mode != 'graceful')
    assert ('kill' in process.actions) == (mode == 'kill')
    previous = process.actions.copy()
    started.close()
    assert process.actions == previous  # Repeated cleanup never targets another process.


def test_failed_cold_boot_still_stops_the_launched_emulator(virtual_setup, monkeypatch):
    process = Process()
    monkeypatch.setattr(emulator.subprocess, 'Popen', lambda *a, **kw: process)
    commands = []
    def adb(*args, serial):
        commands.append((args, serial))
        if args == ('emu', 'kill'):process.returncode = 0
    monkeypatch.setattr(android, 'run_adb', adb)
    with pytest.raises(RuntimeError, match='did not finish'):
        emulator.ensure_emulator(timeout=0)
    assert commands == [(('shell', 'sync'), 'emulator-5554'), (('emu', 'kill'), 'emulator-5554')]
    assert process.returncode == 0


def test_reused_http_session_does_not_boot_or_probe_android(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'padelbot_android_failure_file', tmp_path / 'failure')
    monkeypatch.setattr(emulator, 'ensure_emulator', lambda: pytest.fail('HTTP-only login booted Android'))
    monkeypatch.setattr(android, 'doctor', lambda: pytest.fail('HTTP-only login probed Android'))
    token = object()
    def http_login(path, **kwargs):
        assert kwargs['use_saved_session'] is True
        return token
    monkeypatch.setattr(auth, '_http_login', http_login)
    assert auth.login_via_android(android.AndroidApprover(manual=True)) is token


@pytest.mark.parametrize('failure', [None, 'device', 'login'])
def test_login_releases_emulator_after_exchange_and_on_failures(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(settings, 'padelbot_android_failure_file', tmp_path / 'failure')
    events = []
    def start():
        events.append('start')
        return SimpleNamespace(close=lambda: events.append('stop'))
    monkeypatch.setattr(emulator, 'ensure_emulator', start)
    def doctor():
        if failure == 'device':
            raise RuntimeError('device failed')
        return {'serial': 'emulator-5554', 'app_version': '1'}
    monkeypatch.setattr(android, 'doctor', doctor)
    token = object()
    def http_login(path, *, authenticate_link, **kwargs):
        authenticate_link.prepare()
        authenticate_link.prepare()  # Preparing a selector and its QR page boots once.
        events.append('exchange')
        assert 'stop' not in events
        if failure == 'login':
            raise RuntimeError('login failed')
        return token
    monkeypatch.setattr(auth, '_http_login', http_login)
    if failure:
        with pytest.raises(RuntimeError, match=failure + ' failed'):
            auth.login_via_android(android.AndroidApprover(manual=True))
    else:
        assert auth.login_via_android(android.AndroidApprover(manual=True)) is token
    assert events == (['start', 'stop'] if failure == 'device' else ['start', 'exchange', 'stop'])


def test_cleanup_error_does_not_lose_successful_tokens_or_mask_login_error(tmp_path, monkeypatch):
    def cleanup():
        raise RuntimeError('shutdown failed')
    provider = SimpleNamespace(close=cleanup)
    token = object()
    monkeypatch.setattr(auth, '_http_login', lambda *a, **kw: token)
    assert auth.login_via_android(provider) is token
    def failed_login(*a, **kw):
        raise ValueError('original login error')
    monkeypatch.setattr(auth, '_http_login', failed_login)
    with pytest.raises(ValueError, match='original login error'):
        auth.login_via_android(provider)


@pytest.mark.parametrize('advice_fails', [False, True])
def test_shutdown_releases_linux_file_cache_after_exit_without_modifying_disks(tmp_path, monkeypatch, advice_fails):
    process = Process()
    image = tmp_path / 'registered-disk.img'
    original = b'registered Android state must remain unchanged'
    image.write_bytes(original)
    calls = []
    def advise(fd, offset, length, advice):
        assert process.poll() is not None, 'Never evict the running emulator\'s cache.'
        calls.append(os.fstat(fd).st_size)
        if advice_fails:
            raise OSError('Advisory cache release unavailable')
    monkeypatch.setattr(emulator.os, 'posix_fadvise', advise, raising=False)
    monkeypatch.setattr(emulator.os, 'POSIX_FADV_DONTNEED', 4, raising=False)
    monkeypatch.setattr(android, 'run_adb', lambda *a, **kw: setattr(process, 'returncode', 0))
    started = emulator.StartedEmulator('emulator-5554', process, (tmp_path,))
    started.close()
    started.close()
    assert calls == [len(original)]
    assert image.read_bytes() == original
