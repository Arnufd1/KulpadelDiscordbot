"""Slot watcher: polls bookable-slots periodically and alerts when a watched
slot becomes available within the booking window. Useful for catching
cancellations after the precise weekly opening moment has passed.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from loguru import logger

from .booking import Slot, find_slot_at_time, find_slots
from .client import BackboneClient
from .config import settings
from .rules_store import Rule, RulesStore


@dataclass
class WatchHit:
    rule: Rule
    target_slot_local: datetime
    slot: Slot


def _local_tz() -> ZoneInfo:
    return ZoneInfo(settings.padel_local_tz)


def _next_n_occurrences(rule: Rule, n: int) -> list[datetime]:
    """Up to `n` future Brussels-local datetimes for this rule, within the
    booking window (next 7 days)."""
    tz = _local_tz()
    now = datetime.now(tz)
    hh, mm = (int(x) for x in rule.time_local.split(":"))

    out: list[datetime] = []
    base = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    days_ahead = (rule.day_of_week - base.weekday()) % 7
    target = base + timedelta(days=days_ahead)
    if target <= now:
        target = target + timedelta(days=7)
    for _ in range(n):
        if target - now > timedelta(days=settings.padel_opens_days_ahead):
            break
        out.append(target)
        target = target + timedelta(days=7)
    return out


def check_rule_now(client: BackboneClient, rule: Rule) -> WatchHit | None:
    """Is the next occurrence of this rule currently bookable? Returns the
    slot if yes, None if not."""
    targets = _next_n_occurrences(rule, n=1)
    if not targets:
        return None
    target = targets[0]
    slots = find_slots(client, target_date=datetime.combine(target.date(), datetime.min.time()))
    found = find_slot_at_time(slots, target.strftime("%H:%M"))
    if found is None:
        return None
    if found.raw.get("isAvailable") is False:
        return None
    return WatchHit(rule=rule, target_slot_local=target, slot=found)


async def run_watcher(
    store: RulesStore,
    on_hit: Callable[[WatchHit], Awaitable[None]],
    *,
    poll_interval_s: float = 300.0,    # default 5 min
) -> None:
    """Background task — every `poll_interval_s`, check each enabled rule's
    next occurrence and call `on_hit` if it's now bookable. The bot's caller
    decides whether to auto-book or just alert."""
    logger.info("Watcher started (poll {}s).", poll_interval_s)
    seen: set[tuple[int, str]] = set()  # (rule_id, target_iso) — avoid spamming
    while True:
        try:
            rules = store.list_rules(enabled_only=True)
            for r in rules:
                try:
                    with BackboneClient() as c:
                        hit = await asyncio.to_thread(check_rule_now, c, r)
                except Exception as e:
                    logger.warning("Watcher check failed for rule {}: {}", r.id, e)
                    continue
                if hit is None:
                    continue
                key = (r.id, hit.target_slot_local.isoformat())
                if key in seen:
                    continue
                seen.add(key)
                await on_hit(hit)
            await asyncio.sleep(poll_interval_s)
        except asyncio.CancelledError:
            logger.info("Watcher stopping.")
            raise
        except Exception:
            logger.exception("Watcher loop error — sleeping {}s", poll_interval_s)
            await asyncio.sleep(poll_interval_s)
