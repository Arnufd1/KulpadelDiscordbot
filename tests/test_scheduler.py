import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from padelbot import scheduler
from padelbot.rules_store import RulesStore


@pytest.mark.parametrize("first_failure", [False, True])
@pytest.mark.parametrize("notification_failure", [False, True])
def test_every_rule_at_same_opening_runs_despite_booking_or_notification_failure(tmp_path, monkeypatch, first_failure, notification_failure):
    store = RulesStore(tmp_path / "rules.sqlite3")
    first = store.add_rule(0, "18:00")
    second = store.add_rule(0, "19:00")
    future = store.add_rule(1, "18:00")
    now = datetime.now(ZoneInfo("Europe/Brussels"))
    fire = now - timedelta(milliseconds=100)
    targets = {r.id: now + timedelta(days=7) for r in (first, second, future)}
    def compute(rule):
        return scheduler.FireSpec(rule, targets[rule.id], fire if rule.id != future.id else now + timedelta(days=1))
    attempts, notified = [], []
    def attempt(spec):
        attempts.append(spec.rule.id)
        if first_failure and spec.rule.id == first.id:
            raise RuntimeError("booking failed")
        return True, None, None
    async def on_attempt(spec, ok, result, error):
        notified.append((spec.rule.id, ok))
        if notification_failure and spec.rule.id == first.id:
            raise RuntimeError("Discord unavailable")
    async def stop(delay):
        raise asyncio.CancelledError()
    monkeypatch.setattr(scheduler, "compute_next_fire", compute)
    monkeypatch.setattr(scheduler, "_fire_one_rule", attempt)
    monkeypatch.setattr(scheduler.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler.run_scheduler(store, on_attempt))
    assert attempts == [first.id, second.id]
    assert notified == [(first.id, not first_failure), (second.id, True)]
    history = store.recent_history()
    assert {h.rule_id for h in history} == {first.id, second.id}
    assert all(h.target_slot_iso == targets[h.rule_id].isoformat() for h in history)


@pytest.mark.parametrize("change", ["pause", "remove"])
def test_rule_paused_or_removed_while_waiting_is_not_booked(tmp_path, monkeypatch, change):
    store = RulesStore(tmp_path / "rules.sqlite3")
    first = store.add_rule(0, "18:00")
    second = store.add_rule(0, "19:00")
    fire = datetime.now(ZoneInfo("Europe/Brussels")) + timedelta(seconds=1)
    monkeypatch.setattr(scheduler, "compute_next_fire", lambda rule: scheduler.FireSpec(rule, fire + timedelta(days=7), fire))
    calls, sleeps = [], []
    monkeypatch.setattr(scheduler, "_fire_one_rule", lambda spec: (calls.append(spec.rule.id) or True, None, None))
    async def on_attempt(*args): pass
    async def sleep(delay):
        sleeps.append(delay)
        if len(sleeps) == 1:
            if change == "pause": store.set_enabled(first.id, False)
            else: store.remove_rule(first.id)
        else:
            raise asyncio.CancelledError()
    monkeypatch.setattr(scheduler.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler.run_scheduler(store, on_attempt))
    assert calls == [second.id]
    assert [h.rule_id for h in store.recent_history()] == [second.id]
