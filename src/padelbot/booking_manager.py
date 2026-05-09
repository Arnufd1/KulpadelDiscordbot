"""List + cancel existing bookings via backbone-web-api.

The /bookings GET shape is HAR-confirmed. The cancel endpoint is best-guess
based on REST conventions; we'll learn the real shape the first time the
user runs `/cancel` (the API will return a clear 404/405 if wrong).
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any

from loguru import logger

from .client import BackboneClient


@dataclass
class MyBooking:
    id: int
    sale_id: int | None
    description: str
    start_iso: str
    end_iso: str
    status: int        # 0=open, 2 commonly = cancelled
    paid_for: bool | None
    raw: dict[str, Any]

    @property
    def start_dt(self) -> datetime:
        return datetime.strptime(self.start_iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def list_my_bookings(
    client: BackboneClient,
    *,
    member_id: int,
    upcoming_only: bool = True,
) -> list[MyBooking]:
    """Return the member's upcoming bookings via their /sales (cheaper + correct).

    /bookings is system-wide (~thousands of entries). Instead we go member→sales
    →sale-items(join=booking), which is what the SPA's "My bookings" tab does.
    """
    sales_out = client.get(
        "/sales",
        params={
            "s": {"customerPersonId": member_id, "statusCode": {"$in": [0, 1, 3]}},
            "sort": "date,DESC",
            "limit": 50,
            "page": 1,
        },
    )
    sales = sales_out.get("data", sales_out) if isinstance(sales_out, dict) else sales_out
    sale_ids = [s["id"] for s in (sales or []) if s.get("id")]
    if not sale_ids:
        return []

    # Pull sale-items with booking join. Delcom doesn't accept {"$ne": null},
    # so we filter for booking presence client-side below.
    items_out = client.get(
        "/sale-items",
        params={
            "s": {"saleId": {"$in": sale_ids}},
            "join": "booking",
            "limit": 200,
        },
    )
    items = items_out.get("data", items_out) if isinstance(items_out, dict) else items_out

    now_utc = datetime.now(timezone.utc)
    bookings: list[MyBooking] = []
    seen: set[int] = set()
    for it in items or []:
        b = it.get("booking") or {}
        if not b or not b.get("id"):
            continue
        bid = int(b["id"])
        if bid in seen:
            continue
        seen.add(bid)
        if int(b.get("status", 0)) == 2:  # cancelled
            continue
        if upcoming_only:
            try:
                start = datetime.strptime(b["startDate"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            except (ValueError, KeyError):
                continue
            if start < now_utc:
                continue
        bookings.append(MyBooking(
            id=bid,
            sale_id=it.get("saleId"),
            description=b.get("description") or "",
            start_iso=b.get("startDate") or "",
            end_iso=b.get("endDate") or "",
            status=int(b.get("status", 0)),
            paid_for=b.get("paidFor"),
            raw=b,
        ))
    bookings.sort(key=lambda b: b.start_iso)
    return bookings


def cancel_booking(
    client: BackboneClient,
    *,
    booking_id: int,
    member_id: int,
) -> dict[str, Any]:
    """Best-effort cancel. Endpoint is guessed; if wrong we'll see a clear API
    error and adjust. Common Delcom REST conventions try DELETE first,
    then PATCH."""
    # The SPA likely calls DELETE /participations/<id> (cancel your own seat)
    # or DELETE /bookings/<id> (cancel the whole booking — only if you're owner).
    # Try the participations approach first (less destructive).
    parts_out = client.get(
        "/participations",
        params={"s": {"memberId": member_id, "bookingId": booking_id}},
    )
    parts = parts_out.get("data", parts_out) if isinstance(parts_out, dict) else parts_out
    if parts:
        part_id = parts[0]["id"]
        logger.info("Cancelling participation id={} for booking={}", part_id, booking_id)
        # Try DELETE
        try:
            return client._http.delete(  # type: ignore[attr-defined]
                f"/participations/{part_id}",
                headers=client._headers(),  # type: ignore[attr-defined]
            ).raise_for_status() or {"deleted": True, "participationId": part_id}
        except Exception as e:
            logger.warning("DELETE /participations/{} failed: {}; trying PATCH status=2", part_id, e)
            r = client._http.patch(  # type: ignore[attr-defined]
                f"/participations/{part_id}",
                json={"status": 2},
                headers=client._headers(write=True),  # type: ignore[attr-defined]
            )
            r.raise_for_status()
            return r.json()
    raise RuntimeError(f"No participation found for booking {booking_id}")
