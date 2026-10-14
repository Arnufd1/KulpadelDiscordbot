"""Recurring booking scheduler.

For each enabled rule (e.g. "Mon 18:00 Brussels"):
  - target_slot_dt = next future occurrence of that weekday + time in Brussels
  - fire_at_dt    = target_slot_dt - PADEL_OPENS_DAYS_AHEAD days, at PADEL_OPEN_HOUR_LOCAL
                    (i.e. when the slot becomes bookable), minus PADEL_FIRE_OFFSET_MS

The asyncio loop sleeps until the nearest fire moment, then attempts the booking.
The booking call itself is run in a thread so we don't block the event loop.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Callable, Awaitable
from zoneinfo import ZoneInfo

from loguru import logger

from .booking import (
    BookingResult,
    Slot,
    book as do_book,
    find_slots,
)
from .client import BackboneClient
from .config import settings
from .rules_store import Rule, RulesStore


@dataclass
class FireSpec:
    rule: Rule
    target_slot_local: datetime  # the actual slot start time (Brussels)
    fire_at: datetime            # the moment we should attempt the booking (Brussels)


def _local_tz() -> ZoneInfo:
    return ZoneInfo(settings.padel_local_tz)


def find_rule_slot(slots: list[Slot], rule: Rule, target_day: date, *, now: datetime | None = None) -> Slot | None:
    """Choose the earliest future start within the inclusive local-time window."""
    tz = _local_tz()
    now = now or datetime.now(tz)
    start = datetime.strptime(rule.time_local, "%H:%M").time()
    end = datetime.strptime(rule.end_time_local or rule.time_local, "%H:%M").time()
    matches = []
    for slot in slots:
        local = slot.start_dt.astimezone(tz)
        if (local.date() == target_day and start <= local.time() <= end
                and slot.start_dt > now and slot.raw.get("isAvailable") is not False):
            matches.append(slot)
    return min(matches, key=lambda s: (s.start_dt, s.bookable_product_id, s.bookable_linked_product_id), default=None)


def compute_next_fire(rule: Rule, *, now: datetime | None = None) -> FireSpec:
    """Compute the next firing moment for a rule, in local tz."""
    tz = _local_tz()
    now = (now or datetime.now(tz)).astimezone(tz)
    hh, mm = (int(x) for x in rule.time_local.split(":"))

    # Find next occurrence of weekday+time at or after `now`
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    days_ahead = (rule.day_of_week - target.weekday()) % 7
    target = target + timedelta(days=days_ahead)
    if target <= now:
        target = target + timedelta(days=7)

    fire_dt = target - timedelta(days=settings.padel_opens_days_ahead)
    fire_dt = fire_dt.replace(hour=settings.padel_open_hour_local, minute=0, second=0, microsecond=0)
    fire_dt = fire_dt - timedelta(milliseconds=settings.padel_fire_offset_ms)

    # If we already missed this fire window, advance one week
    if fire_dt <= now:
        target = target + timedelta(days=7)
        fire_dt = fire_dt + timedelta(days=7)

    return FireSpec(rule=rule, target_slot_local=target, fire_at=fire_dt)


def _fire_one_rule(spec: FireSpec) -> tuple[bool, BookingResult | None, str | None]:
    """Synchronous booking attempt — runs in a thread."""
    target_date = spec.target_slot_local.date()
    tz = _local_tz()

    with BackboneClient() as client:
        me = client.me()
        slots = find_slots(client, target_date=datetime.combine(target_date, dtime(), tz))
        slot = find_rule_slot(slots, spec.rule, target_date)
        if slot is None:
            available = ", ".join(
                s.start_dt.astimezone(tz).strftime("%H:%M") for s in slots
            )
            return False, None, f"No slot within {spec.rule.time_label()}. Available: {available or '(none)'}"
        # History and notifications must identify the actual chosen start.
        spec.target_slot_local = slot.start_dt.astimezone(tz)
        # Use the rule's `notes` as the booking label (visible in purchase history)
        label = (spec.rule.notes or None)
        result = do_book(client, slot, member_id=me["id"], me=me, label=label)
    return True, result, None


async def run_scheduler(
    store: RulesStore,
    on_attempt: Callable[[FireSpec, bool, BookingResult | None, str | None], Awaitable[None]],
    *,
    poll_interval_s: float = 30.0,  # how often we recompute next-fire when far away
    booking_lock: asyncio.Lock | None = None,
) -> None:
    """Long-running asyncio task. `on_attempt` is the callback for Discord notifications."""
    logger.info("Scheduler started.")
    booking_lock = booking_lock or asyncio.Lock()
    while True:
        try:
            rules = store.list_rules(enabled_only=True)
            if not rules:
                await asyncio.sleep(poll_interval_s)
                continue
            specs = [compute_next_fire(r) for r in rules]
            specs.sort(key=lambda s: s.fire_at)
            next_spec = specs[0]
            # Capture the whole opening before attempting any bookings. Recomputing
            # after the first attempt would move its peers to the following week.
            due_specs = [spec for spec in specs if spec.fire_at == next_spec.fire_at]
            tz = _local_tz()
            delay = (next_spec.fire_at - datetime.now(tz)).total_seconds()
            if delay > poll_interval_s:
                logger.info(
                    "Next fire: rule {} at {} (in {:.0f}s). Polling again in {}s.",
                    next_spec.rule.label(),
                    next_spec.fire_at.isoformat(),
                    delay,
                    poll_interval_s,
                )
                await asyncio.sleep(poll_interval_s)
                continue
            # We're close. Sleep precisely until the moment.
            if delay > 0:
                logger.info("Sleeping {:.3f}s until fire moment for rule {}",
                            delay, next_spec.rule.label())
                await asyncio.sleep(delay)
            for spec in due_specs:
                async with booking_lock:
                    # Rules may have changed or been fulfilled by the watcher.
                    current_rule = store.get_rule(spec.rule.id)
                    if current_rule is None or not current_rule.enabled:
                        continue
                    if (current_rule.day_of_week, current_rule.time_local, current_rule.end_time_local) != (
                            spec.rule.day_of_week, spec.rule.time_local, spec.rule.end_time_local):
                        continue
                    if store.has_booking_for_day(spec.rule.id, spec.target_slot_local.date()):
                        continue
                    logger.info("FIRING rule {} for slot {}", spec.rule.label(), spec.target_slot_local.isoformat())
                    try:
                        ok, result, err = await asyncio.to_thread(_fire_one_rule, spec)
                    except Exception as e:
                        ok, result, err = False, None, f"{type(e).__name__}: {e}"
                        logger.exception("Booking attempt crashed")
                    store.record(
                        rule_id=spec.rule.id,
                        success=ok,
                        sale_id=(result.sale_id if result else None),
                        booking_id=(result.booking_id if result else None),
                        target_slot_iso=spec.target_slot_local.isoformat(),
                        error=err,
                    )
                try:
                    await on_attempt(spec, ok, result, err)
                except Exception:
                    logger.exception("Booking notification failed for rule {}", spec.rule.id)
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            logger.info("Scheduler stopping.")
            raise
        except Exception:
            logger.exception("Scheduler loop error — sleeping {}s before retry", poll_interval_s)
            await asyncio.sleep(poll_interval_s)
