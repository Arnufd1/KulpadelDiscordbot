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
from datetime import datetime, time as dtime, timedelta
from typing import Callable, Awaitable
from zoneinfo import ZoneInfo

from loguru import logger

from .booking import (
    BookingResult,
    Slot,
    book as do_book,
    find_slot_at_time,
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
    target_time = spec.target_slot_local.strftime("%H:%M")
    tz = _local_tz()
    # tz_offset_hours used by find_slot_at_time — current Brussels offset (incl. DST)
    offset = int(spec.target_slot_local.utcoffset().total_seconds() // 3600)  # type: ignore[union-attr]

    with BackboneClient() as client:
        me = client.me()
        from datetime import datetime as _dt
        slots = find_slots(client, target_date=_dt.combine(target_date, dtime()))
        slot = find_slot_at_time(slots, target_time, tz_offset_hours=offset)
        if slot is None:
            available = ", ".join(
                f"{(s.start_dt.hour + offset) % 24:02d}:{s.start_dt.minute:02d}" for s in slots
            )
            return False, None, f"No slot at {target_time}. Available: {available or '(none)'}"
        # Use the rule's `notes` as the booking label (visible in purchase history)
        label = (spec.rule.notes or None)
        result = do_book(client, slot, member_id=me["id"], me=me, label=label)
    return True, result, None


async def run_scheduler(
    store: RulesStore,
    on_attempt: Callable[[FireSpec, bool, BookingResult | None, str | None], Awaitable[None]],
    *,
    poll_interval_s: float = 30.0,  # how often we recompute next-fire when far away
) -> None:
    """Long-running asyncio task. `on_attempt` is the callback for Discord notifications."""
    logger.info("Scheduler started.")
    while True:
        try:
            rules = store.list_rules(enabled_only=True)
            if not rules:
                await asyncio.sleep(poll_interval_s)
                continue
            specs = [compute_next_fire(r) for r in rules]
            specs.sort(key=lambda s: s.fire_at)
            next_spec = specs[0]
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
            logger.info("FIRING rule {} for slot {}",
                        next_spec.rule.label(),
                        next_spec.target_slot_local.isoformat())
            try:
                ok, result, err = await asyncio.to_thread(_fire_one_rule, next_spec)
            except Exception as e:
                ok, result, err = False, None, f"{type(e).__name__}: {e}"
                logger.exception("Booking attempt crashed")
            store.record(
                rule_id=next_spec.rule.id,
                success=ok,
                sale_id=(result.sale_id if result else None),
                booking_id=(result.booking_id if result else None),
                target_slot_iso=next_spec.target_slot_local.isoformat(),
                error=err,
            )
            await on_attempt(next_spec, ok, result, err)
            # Avoid tight-looping if multiple rules share a fire moment
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            logger.info("Scheduler stopping.")
            raise
        except Exception:
            logger.exception("Scheduler loop error — sleeping {}s before retry", poll_interval_s)
            await asyncio.sleep(poll_interval_s)
