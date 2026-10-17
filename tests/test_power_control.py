"""No infrastructure calls: exercise forced commands and lease lifecycle locally."""
import importlib.util
import io
import json
from pathlib import Path
import runpy
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
LEASE = "a" * 32
SECOND = "b" * 32


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def power(tmp_path, monkeypatch):
    # Production helper is Linux-only; lock mechanics themselves are exercised
    # by Linux/flock, while these tests cover its authorization and state policy.
    monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(LOCK_EX=2, flock=lambda *a: None))
    module = load_script("padel-auth-power")
    module.STATE = tmp_path / "power"
    clock = [10_000.0]
    active = [False]
    calls = []
    def qm(*args):
        calls.append(args)
        if args == ("status", "140"):
            return "status: running" if active[0] else "status: stopped"
        if args == ("start", "140"):
            active[0] = True
        elif args == ("shutdown", "140", "--timeout", "45"):
            active[0] = False
        else:
            pytest.fail("Unexpected hypervisor command: " + repr(args))
        return ""
    monkeypatch.setattr(module, "qm", qm)
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    monkeypatch.delenv("SSH_ORIGINAL_COMMAND", raising=False)
    return SimpleNamespace(module=module, clock=clock, active=active, calls=calls)


@pytest.mark.parametrize("command", ["", "start 140", "stop 141", "ensure root", "ensure " + LEASE + "; reboot", "status; reboot", "status extra", "ensure " + LEASE + "\nshutdown 140", "heartbeat " + "F" * 32])
def test_power_forced_command_rejects_arbitrary_shell_and_other_vm_actions(power, command):
    with pytest.raises(ValueError):
        power.module.main(command)
    assert power.calls == []
    assert not power.module.STATE.exists()


def test_status_and_heartbeat_never_start_the_vm(power, capsys):
    power.module.main("status")
    assert json.loads(capsys.readouterr().out)["status"] == "stopped"
    power.module.main("heartbeat " + LEASE)
    assert all(call == ("status", "140") for call in power.calls)
    assert not power.active[0]


def test_active_and_other_clients_leases_prevent_shutdown(power):
    power.module.main("ensure " + LEASE)
    power.module.main("ensure " + SECOND)
    power.clock[0] += 120
    power.module.main("sweep")
    assert power.active[0]
    power.module.main("release " + LEASE)
    power.clock[0] += 30
    power.module.main("sweep")
    assert power.active[0]
    assert power.calls.count(("start", "140")) == 1
    assert not any(call[0] == "shutdown" for call in power.calls)


def test_final_release_allows_only_graceful_shutdown_after_idle_grace(power):
    power.module.main("ensure " + LEASE)
    power.module.main("release " + LEASE)
    power.clock[0] += 59
    power.module.main("sweep")
    assert power.active[0]
    power.clock[0] += 1
    power.module.main("sweep")
    assert not power.active[0]
    assert ("shutdown", "140", "--timeout", "45") in power.calls
    assert all("stop" not in call and "reset" not in call for call in power.calls)


def test_abandoned_expired_lease_eventually_releases_ram_without_force_stopping(power):
    power.module.main("ensure " + LEASE)
    power.clock[0] += power.module.LEASE_SECONDS + power.module.IDLE_SECONDS + 1
    power.module.main("sweep")
    assert not power.active[0]
    saved = json.loads((power.module.STATE / "leases.json").read_text())
    assert saved["leases"] == {}
    assert power.calls[-2] == ("shutdown", "140", "--timeout", "45")


def test_ssh_key_cannot_invoke_internal_shutdown_sweep(power, monkeypatch):
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "sweep")
    with pytest.raises(ValueError):
        power.module.main("sweep")
    assert power.calls == []


def test_slow_vm_start_gets_a_full_lease_after_start_returns(power, monkeypatch):
    original = power.module.qm
    def slow(*args):
        if args == ("start", "140"):
            power.clock[0] += 150
        return original(*args)
    monkeypatch.setattr(power.module, "qm", slow)
    power.module.main("ensure " + LEASE)
    saved = json.loads((power.module.STATE / "leases.json").read_text())
    assert saved["leases"][LEASE] >= power.clock[0] + power.module.LEASE_SECONDS


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    module = load_script("padel-auth-bridge")
    module.CONFIG = tmp_path / "bridge.json"
    module.CONFIG.write_text(json.dumps({"known_hosts": "/private/known_hosts", "control_key": "/private/control", "worker_key": "/private/worker", "control_host": "power@host", "worker_host": "auth@vm"}))
    commands = []
    def control(config, command):
        commands.append(command)
        return {"status": "running", "leases": 1}
    monkeypatch.setattr(module, "control", control)
    monkeypatch.setattr(module.sys, "stdin", SimpleNamespace(buffer=io.BytesIO()))
    return SimpleNamespace(module=module, commands=commands)


def test_bridge_status_is_read_only_and_does_not_boot_or_open_worker(bridge, monkeypatch, capsys):
    monkeypatch.setattr(bridge.module.subprocess, "Popen", lambda *a, **k: pytest.fail("Status started a worker"))
    bridge.module.main("status")
    assert bridge.commands == ["status"]
    assert json.loads(capsys.readouterr().out)["status"] == "running"


def test_bridge_ssh_requires_pinned_host_keys_and_no_interactive_passwords(bridge):
    config = json.loads(bridge.module.CONFIG.read_text())
    args = bridge.module.ssh_args(config, "worker")
    assert "BatchMode=yes" in args
    assert "StrictHostKeyChecking=yes" in args
    assert "IdentitiesOnly=yes" in args
    assert "UserKnownHostsFile=/private/known_hosts" in args
    assert args[-1] == "auth@vm"


def test_bridge_worker_failure_still_releases_its_lease(bridge, monkeypatch):
    monkeypatch.setattr(bridge.module.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    remote = SimpleNamespace(wait=lambda **k: 3, poll=lambda: 3)
    monkeypatch.setattr(bridge.module.subprocess, "Popen", lambda *a, **k: remote)
    with pytest.raises(SystemExit):
        bridge.module.main("approve")
    assert bridge.commands[0].startswith("ensure ")
    assert bridge.commands[-1] == bridge.commands[0].replace("ensure ", "release ")


def test_bridge_retries_boot_probe_timeout_within_startup_deadline(bridge, monkeypatch):
    probes = []
    def probe(*args, **kwargs):
        probes.append(args[0])
        if len(probes) == 1:
            raise subprocess.TimeoutExpired("ssh", 8)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(bridge.module.subprocess, "run", probe)
    monkeypatch.setattr(bridge.module.time, "sleep", lambda _: None)
    remote = SimpleNamespace(wait=lambda **k: 0, poll=lambda: 0)
    monkeypatch.setattr(bridge.module.subprocess, "Popen", lambda *a, **k: remote)
    bridge.module.main("approve")
    assert len(probes) == 2
    assert bridge.commands[-1].startswith("release ")


def test_bridge_unavailable_vm_releases_lease_without_starting_native_worker(bridge, monkeypatch):
    clock = iter([0, 121])
    monkeypatch.setattr(bridge.module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(bridge.module.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=255))
    monkeypatch.setattr(bridge.module.subprocess, "Popen", lambda *a, **k: pytest.fail("Unreachable VM opened a worker"))
    with pytest.raises(RuntimeError, match="unavailable"):
        bridge.module.main("approve")
    assert bridge.commands[-1].startswith("release ")


def test_bridge_sigterm_cleans_owned_ssh_and_releases_power_lease(bridge, monkeypatch):
    handlers = {}
    monkeypatch.setattr(bridge.module.signal, "signal", lambda kind, handler: handlers.setdefault(kind, handler))
    bridge.module.install_signal_handlers()
    monkeypatch.setattr(bridge.module.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    class Remote:
        returncode = None
        terminated = False
        def wait(self, **kwargs):
            if not self.terminated:
                handlers[signal.SIGTERM](signal.SIGTERM, None)
            return 0
        def poll(self): return self.returncode
        def terminate(self):
            self.terminated = True
            self.returncode = 0
    remote = Remote()
    monkeypatch.setattr(bridge.module.subprocess, "Popen", lambda *a, **k: remote)
    with pytest.raises(SystemExit):
        bridge.module.main("approve")
    assert remote.terminated
    assert bridge.commands[-1].startswith("release ")


@pytest.mark.parametrize("mode", ["", "bash", "approve; reboot", "enroll && cat /etc/shadow"])
def test_worker_forced_entry_rejects_shell_commands(mode, monkeypatch, capsys):
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", mode)
    monkeypatch.setattr("os.execv", lambda *a: pytest.fail("Unexpected worker exec"))
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(SCRIPTS / "padel-auth-worker-entry.py"), run_name="__main__")
    assert stopped.value.code == 1
    assert json.loads(capsys.readouterr().out)["error"] == "Unsupported worker operation"


def test_worker_ping_does_not_start_python_or_android(monkeypatch, capsys):
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "ping")
    monkeypatch.setattr("os.execv", lambda *a: pytest.fail("Ping started a worker"))
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(SCRIPTS / "padel-auth-worker-entry.py"), run_name="__main__")
    assert stopped.value.code == 0
    assert json.loads(capsys.readouterr().out) == {"ready": True}
