import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from padelbot import discord_bot, scheduler, watcher
from padelbot.booking import Slot
from padelbot.rules_store import Rule, RulesStore


TZ = ZoneInfo("Europe/Brussels")
DAY = datetime(2030, 6, 7, tzinfo=TZ)
NOW = DAY.replace(hour=12)


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(scheduler, "datetime", FrozenDateTime)
    monkeypatch.setattr(watcher, "datetime", FrozenDateTime)


class Client:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def me(self): return {"id": 42}


def slot(hour, minute=0, *, day=DAY, available=True):
    start = day.replace(hour=hour, minute=minute)
    return Slot(4385, 4610, start.astimezone(timezone.utc).isoformat(),
                (start + timedelta(minutes=90)).astimezone(timezone.utc).isoformat(),
                None, {"isAvailable": available})


def interval():
    return Rule(1, DAY.weekday(), "18:00", True, "weekly", "21:00")


def test_existing_database_migrates_without_changing_fixed_rules(tmp_path):
    path = tmp_path / "rules.sqlite3"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE rules (id INTEGER PRIMARY KEY, day_of_week INTEGER NOT NULL, "
                  "time_local TEXT NOT NULL, enabled INTEGER NOT NULL, created_at TEXT NOT NULL, notes TEXT)")
        c.execute("INSERT INTO rules VALUES (1, 4, '18:30', 0, '2026-10-09', 'existing')")
    store = RulesStore(path)
    old = store.get_rule(1)
    assert (old.time_local, old.end_time_local, old.enabled, old.notes) == ("18:30", None, False, "existing")
    new = store.add_rule(5, "18:00", end_time_local="21:00")
    reopened = RulesStore(path)
    assert reopened.get_rule(1) == old
    assert reopened.get_rule(new.id).time_label() == "18:00–21:00"


@pytest.mark.parametrize("start,end", [("99:00", "21:00"), ("18:00", "24:00"), ("18:00", "17:00"), ("8:00", "21:00")])
def test_store_rejects_invalid_intervals(tmp_path, start, end):
    store = RulesStore(tmp_path / "rules.sqlite3")
    with pytest.raises(ValueError):
        store.add_rule(4, start, end_time_local=end)
    assert store.list_rules() == []


def test_earliest_free_start_is_chosen_even_on_half_hour_and_unsorted_input():
    early = slot(18, 30)
    slots = [slot(22), slot(21), slot(17, 30), slot(18, available=False), early, slot(19)]
    assert scheduler.find_rule_slot(slots, interval(), DAY.date(), now=NOW) is early
    assert scheduler.find_rule_slot([slot(17, 30), slot(21, 30)], interval(), DAY.date(), now=NOW) is None
    assert scheduler.find_rule_slot([slot(18, day=DAY + timedelta(days=1))], interval(), DAY.date(), now=NOW) is None


@pytest.mark.parametrize("hour", [18, 21])
@pytest.mark.parametrize("month", [1, 7])
def test_both_interval_boundaries_use_brussels_time_in_summer_and_winter(hour, month):
    day = DAY.replace(month=month)
    boundary = slot(hour, day=day)
    assert scheduler.find_rule_slot([boundary], interval(), day.date(), now=day.replace(hour=12)) is boundary


def test_fixed_time_rules_keep_their_exact_start_time():
    rule = Rule(1, DAY.weekday(), "18:30", True, None)
    chosen = slot(18, 30)
    assert scheduler.find_rule_slot([slot(18), slot(19), chosen], rule, DAY.date(), now=NOW) is chosen
    assert scheduler.find_rule_slot([slot(18), slot(19)], rule, DAY.date(), now=NOW) is None


def test_attempt_makes_one_booking_and_reports_the_actual_chosen_time(monkeypatch, frozen):
    chosen = slot(19, 30)
    monkeypatch.setattr(scheduler, "BackboneClient", Client)
    monkeypatch.setattr(scheduler, "find_slots", lambda *a, **kw: [slot(22), chosen, slot(20)])
    writes = []
    result = SimpleNamespace(sale_id=100, booking_id=101)
    monkeypatch.setattr(scheduler, "do_book", lambda client, picked, **kw: writes.append((picked, kw)) or result)
    spec = scheduler.FireSpec(interval(), DAY.replace(hour=18), NOW)
    assert scheduler._fire_one_rule(spec) == (True, result, None)
    assert writes == [(chosen, {"member_id": 42, "me": {"id": 42}, "label": "weekly"})]
    assert spec.target_slot_local == DAY.replace(hour=19, minute=30)
    monkeypatch.setattr(scheduler, "find_slots", lambda *a, **kw: [slot(17), slot(22)])
    assert scheduler._fire_one_rule(spec)[0] is False
    assert len(writes) == 1


def test_watcher_keeps_looking_today_after_the_begin_hour(monkeypatch, frozen):
    monkeypatch.setattr(watcher, "datetime", type("Evening", (FrozenDateTime,), {
        "now": classmethod(lambda cls, tz=None: DAY.replace(hour=19, minute=10))
    }))
    monkeypatch.setattr(scheduler, "datetime", watcher.datetime)
    chosen = slot(19, 30)
    monkeypatch.setattr(watcher, "find_slots", lambda *a, **kw: [slot(18, 30), chosen, slot(22)])
    hit = watcher.check_rule_now(Client(), interval())
    assert hit.slot is chosen
    assert hit.target_slot_local == DAY.replace(hour=19, minute=30)
    assert watcher._next_n_occurrences(interval(), 1) == [DAY.replace(hour=18)]


@pytest.mark.parametrize("success,booking_id", [(True, 101), (False, 101), (True, None)])
def test_fulfilled_week_survives_restart_and_does_not_block_next_week(tmp_path, success, booking_id):
    path = tmp_path / "rules.sqlite3"
    store = RulesStore(path)
    rule = store.add_rule(DAY.weekday(), "18:00", end_time_local="21:00")
    store.record(rule_id=rule.id, success=success, sale_id=100, booking_id=booking_id,
                 target_slot_iso=DAY.replace(hour=19, minute=30).isoformat(), error=None)
    reopened = RulesStore(path)
    assert reopened.has_booking_for_day(rule.id, DAY.date())
    assert not reopened.has_booking_for_day(rule.id, (DAY + timedelta(days=7)).date())


def test_scheduler_and_two_watcher_hits_make_only_one_booking(tmp_path, monkeypatch, frozen):
    store = RulesStore(tmp_path / "rules.sqlite3")
    rule = store.add_rule(DAY.weekday(), "18:00", end_time_local="21:00")
    attempts = []
    def attempt(spec):
        attempts.append(spec.rule.id)
        spec.target_slot_local = DAY.replace(hour=19, minute=30)
        return True, SimpleNamespace(sale_id=100, booking_id=101), None
    monkeypatch.setattr(scheduler, "_fire_one_rule", attempt)
    monkeypatch.setattr(discord_bot, "_fire_one_rule", attempt)
    monkeypatch.setattr(scheduler, "compute_next_fire", lambda r: scheduler.FireSpec(r, DAY.replace(hour=18), NOW - timedelta(seconds=1)))
    async def stop(delay): raise asyncio.CancelledError()
    monkeypatch.setattr(scheduler.asyncio, "sleep", stop)
    async def run():
        bot = discord_bot.PadelBot(store)
        async def no_owner(): return None
        async def notify(*args): pass
        monkeypatch.setattr(bot, "_owner", no_owner)
        async def schedule():
            with pytest.raises(asyncio.CancelledError):
                await scheduler.run_scheduler(store, notify, booking_lock=bot._booking_lock)
        try:
            await asyncio.gather(schedule(),
                bot._on_watch_hit(watcher.WatchHit(rule, DAY.replace(hour=19), slot(19))),
                bot._on_watch_hit(watcher.WatchHit(rule, DAY.replace(hour=20), slot(20))))
            assert attempts == [rule.id]
            assert len(store.recent_history()) == 1
            assert store.recent_history()[0].target_slot_iso == DAY.replace(hour=19, minute=30).isoformat()
        finally:
            await bot.close()
    asyncio.run(run())


def test_watcher_retries_after_slot_disappears_before_booking(tmp_path, monkeypatch, frozen):
    store = RulesStore(tmp_path / "rules.sqlite3")
    rule = store.add_rule(DAY.weekday(), "18:00", end_time_local="21:00")
    monkeypatch.setattr(watcher, "BackboneClient", Client)
    monkeypatch.setattr(watcher, "check_rule_now", lambda *a: watcher.WatchHit(rule, DAY.replace(hour=19), slot(19)))
    hits, polls = [], []
    async def on_hit(hit):
        hits.append(hit)
        return len(hits) > 1
    async def sleep(delay):
        polls.append(delay)
        if len(polls) == 3: raise asyncio.CancelledError()
    monkeypatch.setattr(watcher.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(watcher.run_watcher(store, on_hit))
    assert len(hits) == 2
