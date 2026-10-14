"""Reconcile only the owner's padel cart items; never empty the whole cart."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .booking import Slot


class CartStateUnknownError(RuntimeError):
    pass


def own_cart(client, member_id: int) -> list[dict]:
    items, page = [], 1
    while True:
        out = client.get("/cart-items", params={"s": {"memberId": member_id}, "limit": 100, "page": page})
        batch = out.get("data") if isinstance(out, dict) else out
        if not isinstance(batch, list):
            raise CartStateUnknownError("De winkelwagen kon niet betrouwbaar gecontroleerd worden.")
        items.extend(batch)
        total = out.get("total") if isinstance(out, dict) else None
        if total is None or len(items) >= total:
            break
        if not batch:
            raise CartStateUnknownError("De winkelwagen kon niet volledig opgehaald worden.")
        page += 1
    return [i for i in items if isinstance(i, dict) and i.get("memberId") == member_id]


def _instant(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _key(item):
    params = item.get("params") or {}
    if not isinstance(params, dict):
        return None
    try:
        return (int(params.get("bookableProductId") or item["productId"]),
                int(params["bookableLinkedProductId"]),
                _instant(params["startDate"]), _instant(params["endDate"]))
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def _slot_key(slot: Slot):
    return (slot.bookable_product_id, slot.bookable_linked_product_id,
            _instant(slot.start_iso), _instant(slot.end_iso))


def _removable(item):
    params = item.get("params") or {}
    return item.get("id") and item.get("type") == "product" and _key(item) and not params.get("bookingId")


def _reusable(item, category, label):
    params = item.get("params") or {}
    try:
        return (_removable(item) and Decimal(str(item.get("amount"))) == 1
                and int(item.get("category")) == category
                and not any(params.get(k) for k in ("invitedGuests", "invitedOthers", "invitedMemberEmails", "totalParticipantInvitations"))
                and (params.get("primaryPurchaseMessage") or None) == (label or None))
    except (ValueError, TypeError, ArithmeticError):
        return False


def remove_own_item(client, item: dict, member_id: int) -> None:
    """Recheck ownership and the slot immediately before deleting this item."""
    current = next((i for i in own_cart(client, member_id) if i.get("id") == item.get("id")), None)
    if current is None:
        return
    if not _removable(current) or _key(current) != _key(item):
        raise CartStateUnknownError("Dit winkelwagenitem is gewijzigd; het is niet verwijderd.")
    try:
        client.delete(f"/cart-items/{current['id']}")
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 404:
            raise
    except httpx.TransportError:
        # A timed-out DELETE may already have completed. Read before deciding.
        if any(i.get("id") == current["id"] for i in own_cart(client, member_id)):
            raise CartStateUnknownError("Het verwijderen van het winkelwagenitem is nog niet bevestigd.") from None
        return
    if any(i.get("id") == current["id"] for i in own_cart(client, member_id)):
        raise CartStateUnknownError("Het winkelwagenitem staat nog in de winkelwagen.")


def cleanup_slot_cart(client, slot: Slot, member_id: int) -> None:
    key = _slot_key(slot)
    for item in own_cart(client, member_id):
        if _removable(item) and _key(item) == key:
            remove_own_item(client, item, member_id)


def _prepare_cart(client, slot, member_id, category, label):
    key = _slot_key(slot)
    items = own_cart(client, member_id)
    exact = [i for i in items if _key(i) == key]
    reusable = next((i for i in exact if _reusable(i, category, label)), None)
    conflicts = [i for i in items if _removable(i) and i is not reusable
                 and _key(i)[2] <= key[3] and _key(i)[3] >= key[2]]
    if conflicts:
        # Resolve padel activity IDs before touching an overlapping other court.
        from .booking import _padel_activity_product_ids, _padel_tag_id
        padel_ids = set(_padel_activity_product_ids(client, _padel_tag_id(client)))
        for item in conflicts:
            if _key(item) == key or _key(item)[1] in padel_ids:
                remove_own_item(client, item, member_id)
    if reusable:
        fresh = next((i for i in own_cart(client, member_id) if i.get("id") == reusable["id"]), None)
        if fresh is not None and _key(fresh) == key and _reusable(fresh, category, label):
            return fresh
    return None


def ensure_cart_item(client, slot: Slot, member_id: int, body: dict, *, category: int, label=None) -> dict:
    """Reuse an exact own item or replace conflicting unpaid padel items.

    After a known cart-conflict 403, re-read once to handle concurrent changes.
    Never replay a request on an unknown network outcome without read evidence.
    """
    existing = _prepare_cart(client, slot, member_id, category, label)
    if existing is not None:
        return existing
    for attempt in range(2):
        try:
            response = client.post("/cart-items", body)
        except httpx.TransportError:
            candidates = own_cart(client, member_id)
            found = next((i for i in candidates if _key(i) == _slot_key(slot) and _reusable(i, category, label)), None)
            if found is not None:
                return found
            raise CartStateUnknownError("Winkelwagenwijziging niet bevestigd; er is geen nieuwe betaalpoging gestart.") from None
        except httpx.HTTPStatusError as error:
            try:
                message = str(error.response.json().get("message", "")).lower()
            except Exception:
                message = ""
            conflict = error.response.status_code == 403 and any(
                text in message for text in ("already in your cart", "not allow simultaneous", "not allowed simultaneous"))
            if not conflict or attempt:
                raise
            before = own_cart(client, member_id)
            existing = _prepare_cart(client, slot, member_id, category, label)
            if existing is not None:
                return existing
            after = own_cart(client, member_id)
            if {i.get("id") for i in before} == {i.get("id") for i in after}:
                # No own cart conflict was repaired: this may be a real booking.
                raise RuntimeError("KUL blokkeert een overlappende reservatie of ander aankoopitem; er is geen dubbele boeking gemaakt.") from None
            continue
        data = response.get("data", response) if isinstance(response, dict) else None
        if (isinstance(data, dict) and data.get("memberId") == member_id
                and _key(data) == _slot_key(slot) and _reusable(data, category, label)):
            return data
        found = next((i for i in own_cart(client, member_id)
                      if _key(i) == _slot_key(slot) and _reusable(i, category, label)), None)
        if found is not None:
            return found
        raise CartStateUnknownError("Het gekozen winkelwagenitem kon niet bevestigd worden; er is nog niets betaald.")
    raise CartStateUnknownError("De winkelwagen kon niet hersteld worden.")
