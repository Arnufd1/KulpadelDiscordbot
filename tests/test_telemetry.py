import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
import pytest

from padelbot import auth, telemetry
from padelbot.config import settings
from padelbot.idp import SessionExpiredError
from padelbot.store import TokenBundle, TokenStore


USER = "a" * 32
OTHER = "b" * 32
JOB = "c" * 32


@pytest.fixture
def metrics(tmp_path, monkeypatch):
    path = tmp_path / "private" / "metrics.sqlite3"
    monkeypatch.setenv("PADELWEB_TELEMETRY_DB", str(path))
    monkeypatch.setenv("PADELWEB_USER_ID", USER)
    monkeypatch.setenv("PADELWEB_JOB_ID", JOB)
    monkeypatch.setenv("PADELWEB_AUTH_CONTEXT", "login")
    return path


def event(number, *, user=USER, operation="android_login", context="renewal", outcome="success", native=1, ago=100):
    started = time.time() - ago
    return {"id": f"{number:032x}", "user_id": user, "job_id": JOB,
            "operation": operation, "context": context, "started": started,
            "finished": started + 1, "duration_seconds": 1.0, "outcome": outcome,
            "error_category": None, "native_used": native, "peak_rss_mb": None, "job_kind": None}


def test_instrumentation_never_persists_exception_credentials(metrics):
    secret = "PIN1234-https://private.example/token?secret=do-not-save"
    with pytest.raises(httpx.ConnectError):
        with telemetry.auth_context("renewal"), telemetry.measure("silent_session"):
            raise httpx.ConnectError(secret)
    rows = telemetry.export_events(metrics)
    assert len(rows) == 1
    assert rows[0]["error_category"] == "network"
    assert rows[0]["context"] == "renewal"
    assert rows[0]["job_id"] == JOB
    assert secret not in json.dumps(rows)
    assert "job_id" not in telemetry.metrics_summary(metrics)["recent"][0]
    with telemetry.measure(secret):
        pass
    assert len(telemetry.export_events(metrics)) == 1


def test_disabled_or_unwritable_measurements_never_change_auth_result(tmp_path, monkeypatch):
    monkeypatch.delenv("PADELWEB_TELEMETRY_DB", raising=False)
    with telemetry.measure("android_login") as measurement:
        measurement.mark_native()
    assert measurement.path is None
    monkeypatch.setenv("PADELWEB_TELEMETRY_DB", str(tmp_path))
    with telemetry.measure("android_login"):
        result = object()
    assert result is not None
    with pytest.raises(RuntimeError, match="original error"):
        with telemetry.measure("silent_session"):
            raise RuntimeError("original error")


def test_cadence_is_per_profile_and_never_invented_from_enrollment_or_probes(metrics):
    rows = [event(1, ago=700), event(2, ago=590),
            event(3, user=OTHER, ago=680), event(4, user=OTHER, ago=470),
            event(5, ago=400, context="probe"), event(6, ago=390, context="enrollment"),
            event(7, ago=380, native=0), event(8, ago=370, outcome="failed")]
    assert telemetry.ingest_events(metrics, rows) == len(rows)
    result = telemetry.metrics_summary(metrics)
    assert result["cadence"]["sample_count"] == 2
    assert result["cadence"]["median_seconds"] == pytest.approx(160, abs=.01)
    assert result["totals"]["mfa_successes"] == 4
    assert result["totals"]["mfa_failures"] == 1
    assert result["totals"]["silent_successes"] == 1
    assert result["profiles"][0]["cadence"]["sample_count"] == 1
    filtered = telemetry.metrics_summary(metrics, user_id=OTHER)
    assert len(filtered["profiles"]) == 1
    assert filtered["cadence"]["median_seconds"] == pytest.approx(210, abs=.01)
    assert result["daily"] == sorted(result["daily"], key=lambda row: row["date"])
    assert telemetry.metrics_summary(metrics, user_id="untrusted email")["cadence"]["median_seconds"] is None


def test_single_success_is_insufficient_to_claim_a_reauthentication_interval(metrics):
    assert telemetry.ingest_events(metrics, [event(1)]) == 1
    result = telemetry.metrics_summary(metrics)
    assert result["totals"]["mfa_successes"] == 1
    assert result["cadence"]["sample_count"] == 0
    assert result["cadence"]["median_seconds"] is None


def test_daily_labels_use_belgian_local_date_while_wire_timestamps_stay_utc(metrics, monkeypatch):
    # 22:45 UTC is already the next day in Belgium during summer time.
    instant = datetime(2026, 10, 17, 22, 45, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(telemetry.time, "time", lambda: instant)
    assert telemetry.ingest_events(metrics, [event(1, ago=300)]) == 1
    result = telemetry.metrics_summary(metrics)
    assert result["daily"][-1]["date"] == "2026-10-18"
    assert result["daily"][-1]["mfa_successes"] == 1
    assert result["window"]["until"] == instant


def test_remote_event_export_is_bounded_idempotent_and_updates_only_unfinished_spans(metrics, tmp_path):
    target = tmp_path / "central.sqlite3"
    measurement = telemetry.measure("emulator")
    pending = telemetry.export_events(metrics, job_id=JOB, limit=1)
    assert len(pending) == 1 and pending[0]["outcome"] == "running"
    assert telemetry.ingest_events(target, pending) == 1
    assert telemetry.ingest_events(target, pending) == 0
    measurement.peak_rss_mb = 123.5
    measurement.finish()
    completed = telemetry.export_events(metrics, job_id=JOB)
    assert telemetry.ingest_events(target, completed) == 1
    assert telemetry.ingest_events(target, completed) == 0
    assert telemetry.ingest_events(target, pending) == 0
    assert telemetry.metrics_summary(target)["performance"]["peak_rss_mb"] == 123.5
    assert telemetry.export_events(metrics, job_id="wrong") == []
    assert telemetry.export_events(metrics, since=time.time() + 100) == []


@pytest.mark.parametrize("change", [
    {"pin": "1234"}, {"operation": "https://private.example"}, {"user_id": "person@example.com"},
    {"error_category": "raw provider failure"}, {"started": float("nan")},
    {"outcome": "secret"}, {"peak_rss_mb": -1}, {"job_kind": "unvalidated kind"},
])
def test_remote_metrics_import_accepts_only_fixed_metadata_schema(metrics, change):
    row = event(1)
    row.update(change)
    assert telemetry.ingest_events(metrics, [row]) == 0


def test_parallel_process_style_writes_preserve_events_and_prune_old_data(metrics):
    with telemetry.measure("refresh_token"):
        pass
    def worker(_):
        with telemetry.measure("silent_session"):
            pass
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(worker, range(24)))
    assert len(telemetry.export_events(metrics)) == 25
    with sqlite3.connect(metrics) as c:
        c.execute("UPDATE auth_events SET started=? WHERE operation='refresh_token'", (time.time() - 91 * 86400,))
    telemetry._pruned.pop(str(metrics), None)
    with telemetry.measure("silent_session"):
        pass
    with sqlite3.connect(metrics) as c:
        assert c.execute("SELECT COUNT(*) FROM auth_events").fetchone()[0] == 25


def test_job_measurements_are_idempotent_and_distinguish_queue_delay_from_runtime(metrics):
    now = time.time()
    kwargs = dict(user_id=USER, job_id=JOB, kind="slots", created=now - 8, started=now - 3, finished=now, status="done")
    assert telemetry.record_job(metrics, **kwargs) == 2
    assert telemetry.record_job(metrics, **kwargs) == 0
    result = telemetry.metrics_summary(metrics)
    assert result["totals"]["jobs_completed"] == 1
    assert result["performance"]["mean_job_seconds"] == 3
    assert result["performance"]["mean_job_wait_seconds"] == 5


def test_http_session_reuse_does_not_count_as_native_mfa(metrics, monkeypatch):
    monkeypatch.setattr(auth, "_http_login", lambda *a, **k: "saved-session")
    assert auth.login_via_android(lambda _: pytest.fail("HTTP reuse invoked native auth")) == "saved-session"
    result = telemetry.metrics_summary(metrics)
    assert result["totals"]["silent_successes"] == 1
    assert result["totals"]["mfa_successes"] == 0
    assert result["totals"]["emulator_starts"] == 0


@pytest.mark.parametrize("failure", [False, True])
def test_mfa_success_requires_successful_oidc_flow_and_preserves_prepare_and_cleanup(metrics, monkeypatch, failure):
    calls = []
    class Approver:
        def prepare(self): calls.append("prepare")
        def __call__(self, _): calls.append("approve")
        def close(self): calls.append("close")
        def on_failure(self): calls.append("failure")
    def login(*args, authenticate_link, **kwargs):
        authenticate_link.prepare()
        authenticate_link("unlogged challenge")
        if failure:
            raise RuntimeError("unlogged exchange error")
        return "new token"
    monkeypatch.setattr(auth, "_http_login", login)
    if failure:
        with pytest.raises(RuntimeError):
            auth.login_via_android(Approver())
    else:
        assert auth.login_via_android(Approver()) == "new token"
    assert calls == (["prepare", "approve", "failure", "close"] if failure else ["prepare", "approve", "close"])
    totals = telemetry.metrics_summary(metrics)["totals"]
    assert totals["mfa_successes"] == int(not failure)
    assert totals["mfa_failures"] == int(failure)


def test_deferred_read_renewal_never_starts_native_device_and_preserves_saved_tokens(metrics, tmp_path, monkeypatch):
    from padelbot import android
    monkeypatch.setenv("PADELWEB_DEFER_AUTH", "true")
    monkeypatch.setattr(settings, "padelbot_android_autologin", True)
    store = TokenStore(tmp_path / "key", tmp_path / "token")
    store.init_key()
    store.save(TokenBundle("old", "", None, "Bearer", 1, "openid"))
    monkeypatch.setattr(auth, "silent_refresh_via_session", lambda *a, **k: (_ for _ in ()).throw(SessionExpiredError("expired")))
    monkeypatch.setattr(android, "AndroidApprover", lambda: pytest.fail("Read task booted native auth"))
    with pytest.raises(auth.NativeAuthRequired):
        auth.renew_access_token(store)
    assert store.load().access_token == "old"
