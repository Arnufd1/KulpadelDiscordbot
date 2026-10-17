"""One availability interval with its original, bookable court choices."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Iterable
from zoneinfo import ZoneInfo


def _instant(value: str) -> datetime:
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Een tijdslot moet een tijdzone bevatten.")
    return instant.astimezone(timezone.utc)


def _utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _natural(value: object) -> tuple:
    return tuple((1, int(part)) if part.isdigit() else (0, part.casefold())
                 for part in re.split(r"(\d+)", str(value)))


def group_slots(slots: Iterable[dict]) -> list[dict]:
    """Group equal start *and* end instants, retaining exact provider choices.

    UTC IDs are independent of timezone spelling and feed order. Courts are
    naturally ordered (1, 2, 10) and repeated product/court choices are removed
    only within the same interval. Adjacent or overlapping intervals stay apart.
    """
    intervals: dict[tuple[datetime, datetime], list[dict]] = {}
    for slot in slots:
        start, end = _instant(slot["start"]), _instant(slot["end"])
        if end <= start:
            raise ValueError("Een tijdslot moet eindigen na het begint.")
        intervals.setdefault((start, end), []).append(slot)
    grouped = []
    for (start, end), choices in sorted(intervals.items()):
        ordered = sorted(choices, key=lambda slot: (
            _natural(slot.get("name", f"Veld {slot['product']}")),
            _natural(slot["product"]), _natural(slot["court"]),
            slot["start"], slot["end"],
            json.dumps(slot, sort_keys=True, ensure_ascii=False, default=str)))
        courts, seen = [], set()
        for slot in ordered:
            identity = (str(slot["product"]), str(slot["court"]))
            if identity not in seen:
                seen.add(identity)
                courts.append(slot)
        grouped.append({"id": f"{_utc(start)}/{_utc(end)}",
                        "start": courts[0]["start"], "end": courts[0]["end"],
                        "courts": courts})
    return grouped


def interval_label(group: dict, timezone_name: str = "Europe/Brussels") -> str:
    """Display the complete local interval, including offsets on DST overlap."""
    zone = ZoneInfo(timezone_name)
    start, end = _instant(group["start"]).astimezone(zone), _instant(group["end"]).astimezone(zone)
    ambiguous = any(value.replace(fold=0).utcoffset() != value.replace(fold=1).utcoffset()
                    for value in (start, end))
    if ambiguous or start.utcoffset() != end.utcoffset():
        return f"{start:%H:%M %Z}–{end:%H:%M %Z}"
    return f"{start:%H:%M}–{end:%H:%M}"
