"""List own bookings and cancel reservations using the USC frontend API."""
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
    """Cancel the own reservation's sale, as USC's booking details modal does.

    Paid sales may create a credit sale. A combined purchase must be handled
    in USC so cancelling one booking cannot cancel other purchased items.
    Never fall back to another write after an uncertain response.
    """
    mine = list_my_bookings(client, member_id=member_id, upcoming_only=True)
    booking = next((b for b in mine if b.id == booking_id), None)
    if booking is None or not booking.sale_id:
        raise RuntimeError("Deze reservatie is niet meer beschikbaar in je eigen boekingen.")
    sale_id = booking.sale_id
    response = client.get(f"/sales/{sale_id}", params={
        "s": {"id": sale_id, "customerPersonId": member_id},
    })
    sale = response.get("data", response)
    if sale.get("id") != sale_id or sale.get("customerPersonId") != member_id:
        raise RuntimeError("De aankoop hoort niet bij het aangemelde account.")
    items_out = client.get(f"/sales/{sale_id}/sale-items", params={"join": "booking"})
    items = items_out.get("data", []) if isinstance(items_out, dict) else items_out
    if not items or any((it.get("bookingId") or (it.get("booking") or {}).get("id")) != booking_id for it in items):
        raise RuntimeError("Deze reservatie hoort bij een gecombineerde aankoop. Annuleer ze via KU Leuven Sport.")
    logger.info("Cancelling own sale id={} for booking={}", sale_id, booking_id)
    response = client.patch(f"/sales/{sale_id}", {"statusCode": 2})
    sale = response.get("data", response)
    if sale.get("id") != sale_id or not (sale.get("statusCode") == 2 or sale.get("creditSales")):
        raise RuntimeError("Annulering nog niet bevestigd. Vernieuw je boekingen voor je opnieuw probeert.")
    if any(b.id == booking_id for b in list_my_bookings(client, member_id=member_id, upcoming_only=True)):
        raise RuntimeError("Annulering verwerkt, maar de reservatie staat nog in het overzicht. Vernieuw je boekingen.")
    if booking.raw.get("productId") and booking.raw.get("linkedProductId"):
        from .booking import Slot
        from .cart import cleanup_slot_cart
        slot = Slot(int(booking.raw["productId"]), int(booking.raw["linkedProductId"]),
                    booking.start_iso, booking.end_iso, None, {})
        try:
            cleanup_slot_cart(client, slot, member_id)
        except Exception as error:
            logger.warning("Cancelled booking cart cleanup failed: {}", type(error).__name__)
    return response
