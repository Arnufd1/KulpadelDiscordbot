from pathlib import Path
from types import SimpleNamespace

import pytest

from padelbot import android, emulator
from padelbot.config import settings


@pytest.fixture
def virtual_setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, 'padelbot_android_emulator_autostart', True)
    monkeypatch.setattr(settings, 'padelbot_android_serial', 'emulator-5554')
    monkeypatch.setattr(settings, 'padelbot_android_emulator_name', 'KulPadelAuth')
    monkeypatch.setattr(settings, 'padelbot_android_emulator_home', tmp_path / 'avds')
    executable = tmp_path / '.tools/android/emulator' / ('emulator.exe' if emulator.os.name == 'nt' else 'emulator')
    executable.parent.mkdir(parents=True)
    executable.touch()
    settings.padelbot_android_emulator_home.mkdir()
    (settings.padelbot_android_emulator_home / 'KulPadelAuth.ini').write_text('path=registered-avd')
    monkeypatch.setattr(android, 'list_devices', lambda: [])
    monkeypatch.setattr(android, 'run_adb', lambda *args, **kwargs: '1')
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
    emulator.ensure_emulator()
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
