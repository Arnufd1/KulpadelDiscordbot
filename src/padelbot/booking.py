"""Padel slot discovery + reservation.

Mapped from the real purchase HAR. Two-step booking (cart + sale) reserves the
slot. /payments is left to the user — the bot pings Discord with a "go pay
within N minutes" message.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

from .client import BackboneClient

# Constants from HAR — these are stable system values, not per-user.
SALES_PERSON_ID = 148113   # self-service system user
PAY_METHOD_CARD = 6
SITE_ID = 0
PADEL_TAG_NAME_HINTS = ("padel",)  # tag.description matched case-insensitively


@dataclass
class Slot:
    """One bookable padel time slot."""
    bookable_product_id: int       # parent activity product id (e.g. 4383)
    bookable_linked_product_id: int  # specific slot/court (e.g. 4535)
    start_iso: str                 # e.g. "2026-05-13T09:30:00.000Z"
    end_iso: str
    price: float | None
    raw: dict[str, Any]            # full record from /products/bookable-slots
    is_holiday: bool = False       # this day is a Belgian public holiday
    day_has_bookings: bool = False  # any padel court has at least one booking today

    @property
    def start_dt(self) -> datetime:
        return datetime.strptime(self.start_iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)

    def label(self) -> str:
        d = self.start_dt
        return f"{d.strftime('%Y-%m-%d %H:%M')} UTC (slot {self.bookable_linked_product_id})"


def _padel_tag_id(client: BackboneClient) -> int:
    """Resolve the padel activity tag id. Prefer EXACT description match
    ('padel') over substring matches ('padelcourt', 'padelveld') so we get
    the parent activity tag (id 72 in this account) and not a child sub-tag.
    """
    tags = client.get("/public/tags", params={"join": "documents"})
    items = tags.get("data", tags) if isinstance(tags, dict) else tags

    candidates: list[tuple[bool, int, dict, list[str]]] = []  # (exact?, id, raw, descs)
    for t in items or []:
        descs = [
            (t.get(k) or "").strip()
            for k in ("description", "name", "description_en", "description_nl",
                      "description_de", "description_fr", "description_es")
        ]
        descs = [d for d in descs if d]
        if not descs:
            continue
        for h in PADEL_TAG_NAME_HINTS:
            joined = " ".join(d.lower() for d in descs)
            if h not in joined:
                continue
            exact = any(d.lower() == h for d in descs)
            candidates.append((exact, int(t["id"]), t, descs))
            break

    if not candidates:
        raise RuntimeError(f"No tag matching {PADEL_TAG_NAME_HINTS} in /public/tags")

    # Exact matches first, then lowest id
    candidates.sort(key=lambda c: (not c[0], c[1]))
    exact_flag, tid, _, descs = candidates[0]
    logger.info("Resolved padel tag: id={} descs={!r} (exact={})", tid, descs, exact_flag)
    return tid


def _padel_activity_product_ids(client: BackboneClient, tag_id: int) -> list[int]:
    """The set of product IDs for the padel activity (used to filter bookable-slots).
    Real response shape: {"productIdsByTagId": {"<tag_id>": [<id>, <id>, ...]}}
    """
    out = client.get(
        "/tags/query/availableActivityTagsAndProductIds",
        params={"tagIds": tag_id},
    )
    ids: list[int] = []

    if isinstance(out, dict) and "productIdsByTagId" in out:
        m = out["productIdsByTagId"] or {}
        for k in (str(tag_id), tag_id):
            if k in m:
                ids = list(m[k] or [])
                break

    if not ids:
        # Fallback shapes for resilience
        data = out.get("data") if isinstance(out, dict) else out
        for entry in data or []:
            for k in ("productIds", "activityProductIds", "ids"):
                if k in entry:
                    ids.extend(entry[k])

    if not ids:
        raise RuntimeError(f"Could not extract product ids from tag {tag_id}: {out!r}")
    return sorted(set(int(i) for i in ids))


def find_slots(
    client: BackboneClient,
    *,
    target_date: datetime,
    activity: str = "padel",
    location_id: int | None = None,
) -> list[Slot]:
    """Return all bookable padel slots whose start date falls on `target_date` (local day)."""
    from zoneinfo import ZoneInfo

    tag_id = _padel_tag_id(client)
    activity_ids = _padel_activity_product_ids(client, tag_id)

    # The SPA filters by startDate.$gte (Brussels-day start in UTC) AND
    # endDate.$lte (Brussels-day end in UTC). Match that exactly — the API
    # silently returns nothing if you use $lt / mismatched operators.
    tz = ZoneInfo("Europe/Brussels")
    if target_date.tzinfo is None:
        local_day = target_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz)
    else:
        local_day = target_date.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    day_start_utc = local_day.astimezone(timezone.utc)
    day_end_utc = (local_day + timedelta(days=1)).astimezone(timezone.utc) - timedelta(milliseconds=1)

    def _iso_z(d: datetime) -> str:
        return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"

    s_filter: dict[str, Any] = {
        "activityProductIds": {"$in": activity_ids},
        "startDate": {"$gte": _iso_z(day_start_utc)},
        "endDate":   {"$lte": _iso_z(day_end_utc)},
    }
    if location_id is not None:
        s_filter["locationId"] = location_id

    out = client.get("/products/bookable-slots", params={"s": s_filter})
    raw_slots = out.get("data", out) if isinstance(out, dict) else out

    parent_ids = sorted({int(r["bookableProductId"]) for r in (raw_slots or []) if r.get("bookableProductId")})
    booked = _booked_keys(client, parent_ids=parent_ids, start_utc=day_start_utc, end_utc=day_end_utc)

    slots: list[Slot] = []
    seen: set[tuple[str, int]] = set()
    for r in raw_slots or []:
        sid_parent = r.get("bookableProductId") or r.get("activityProductId") or r.get("productId")
        sid_linked = r.get("bookableLinkedProductId") or r.get("linkedProductId") or r.get("id")
        start = r.get("startDate") or r.get("start")
        end = r.get("endDate") or r.get("end")
        if not (sid_parent and sid_linked and start and end):
            continue
        # Trust isAvailable — that's the real "can I book this" signal,
        # same one the KU Leuven app uses.
        if r.get("isAvailable") is False:
            continue
        key = (start, int(sid_parent))
        if key in seen:
            continue
        seen.add(key)
        slots.append(Slot(
            bookable_product_id=int(sid_parent),
            bookable_linked_product_id=int(sid_linked),
            start_iso=start,
            end_iso=end,
            price=r.get("price") or r.get("amount"),
            raw=r,
        ))
    slots.sort(key=lambda s: s.start_iso)
    return slots


def find_slots_window(
    client: BackboneClient,
    *,
    start_date: datetime,
    days: int = 7,
) -> list[Slot]:
    """All bookable padel slots starting in [start_date, start_date+days) (Brussels-local days)."""
    from zoneinfo import ZoneInfo

    tag_id = _padel_tag_id(client)
    activity_ids = _padel_activity_product_ids(client, tag_id)

    tz = ZoneInfo("Europe/Brussels")
    if start_date.tzinfo is None:
        local_start = start_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz)
    else:
        local_start = start_date.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=days)
    start_utc = local_start.astimezone(timezone.utc)
    end_utc = local_end.astimezone(timezone.utc) - timedelta(milliseconds=1)

    def _iso_z(d: datetime) -> str:
        return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"

    s_filter: dict[str, Any] = {
        "activityProductIds": {"$in": activity_ids},
        "startDate": {"$gte": _iso_z(start_utc)},
        "endDate":   {"$lte": _iso_z(end_utc)},
    }

    out = client.get("/products/bookable-slots", params={"s": s_filter})
    raw_slots = out.get("data", out) if isinstance(out, dict) else out

    # Source of truth for availability is the `isAvailable` field on each
    # bookable-slots row — that's what the real KU Leuven app uses. Cross-
    # referencing /bookings was over-aggressive (filtered group slots where
    # joining is still allowed). We still query bookings to power the
    # "is gym open today" signal for the holiday warning.
    parent_ids = sorted({int(r["bookableProductId"]) for r in (raw_slots or []) if r.get("bookableProductId")})
    booked = _booked_keys(client, parent_ids=parent_ids, start_utc=start_utc, end_utc=end_utc)
    days_with_bookings: set[str] = set()
    for _pid, sd in booked:
        try:
            dt = datetime.strptime(sd[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).astimezone(tz)
            days_with_bookings.add(dt.strftime("%Y-%m-%d"))
        except ValueError:
            pass

    # Compute holidays in window (any year touched).
    years = {start_utc.year, end_utc.year, local_start.year, local_end.year}
    holidays: set[str] = set()
    for y in years:
        holidays |= belgian_public_holidays(y)

    # API enforces "168 hours ahead" booking horizon from NOW. Filter to avoid
    # surfacing slots that would 403 on /cart-items.
    booking_horizon = datetime.now(timezone.utc) + timedelta(hours=168)

    slots: list[Slot] = []
    seen: set[tuple[str, int]] = set()
    for r in raw_slots or []:
        sid_parent = r.get("bookableProductId") or r.get("activityProductId") or r.get("productId")
        sid_linked = r.get("bookableLinkedProductId") or r.get("linkedProductId") or r.get("id")
        start = r.get("startDate") or r.get("start")
        end = r.get("endDate") or r.get("end")
        if not (sid_parent and sid_linked and start and end):
            continue
        # Trust isAvailable — that's the real "can I book this" signal,
        # same one the KU Leuven app uses.
        if r.get("isAvailable") is False:
            continue
        # Beyond 168h booking horizon — would 403 on /cart-items
        try:
            slot_start = datetime.strptime(start[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            if slot_start > booking_horizon:
                continue
        except ValueError:
            pass
        key = (start, int(sid_parent))
        if key in seen:
            continue
        seen.add(key)

        try:
            local_day = (
                datetime.strptime(start[:19], "%Y-%m-%dT%H:%M:%S")
                .replace(tzinfo=timezone.utc).astimezone(tz)
                .strftime("%Y-%m-%d")
            )
        except ValueError:
            local_day = ""

        slots.append(Slot(
            bookable_product_id=int(sid_parent),
            bookable_linked_product_id=int(sid_linked),
            start_iso=start,
            end_iso=end,
            price=r.get("price") or r.get("amount"),
            raw=r,
            is_holiday=local_day in holidays,
            day_has_bookings=local_day in days_with_bookings,
        ))
    slots.sort(key=lambda s: s.start_iso)
    return slots


def _easter_sunday(year: int) -> "date":
    """Anonymous Gregorian algorithm."""
    from datetime import date
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def belgian_public_holidays(year: int) -> set[str]:
    """Returns YYYY-MM-DD strings for Belgian public holidays in the year."""
    from datetime import date, timedelta
    easter = _easter_sunday(year)
    return {
        d.isoformat() for d in [
            date(year, 1, 1),                  # New Year
            easter + timedelta(days=1),        # Easter Monday
            date(year, 5, 1),                  # Labour Day
            easter + timedelta(days=39),       # Ascension Thursday
            easter + timedelta(days=50),       # Whit Monday
            date(year, 7, 21),                 # National Day
            date(year, 8, 15),                 # Assumption
            date(year, 11, 1),                 # All Saints
            date(year, 11, 11),                # Armistice
            date(year, 12, 25),                # Christmas
        ]
    }


def _site_closed_days(
    client: BackboneClient,
    *,
    start_utc: datetime,
    end_utc: datetime,
) -> set[str]:
    """Return Brussels-local YYYY-MM-DD strings for days that have site-wide
    maintenance/closure bookings (e.g. Ascension Day). The /bookings endpoint
    returns 'Maintenance' bookings on closure days even when the courts
    themselves don't have explicit closure entries."""
    from zoneinfo import ZoneInfo

    def _iso_z(d: datetime) -> str:
        return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"

    out = client.get(
        "/bookings",
        params={
            "s": {
                "startDate": {"$gte": _iso_z(start_utc)},
                "endDate":   {"$lte": _iso_z(end_utc)},
            },
            "limit": 500,
        },
    )
    data = out.get("data", out) if isinstance(out, dict) else out
    tz = ZoneInfo("Europe/Brussels")
    closed: set[str] = set()
    for b in data or []:
        desc = (b.get("description") or "").lower()
        if "maintenance" not in desc and "maitenance" not in desc:
            continue
        sd = b.get("startDate")
        if not sd:
            continue
        try:
            local = datetime.strptime(sd[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).astimezone(tz)
        except ValueError:
            continue
        closed.add(local.strftime("%Y-%m-%d"))
    return closed


def _booked_keys(
    client: BackboneClient,
    *,
    parent_ids: list[int],
    start_utc: datetime,
    end_utc: datetime,
) -> set[tuple[int, str]]:
    """Returns the set of (productId, startDate-iso-Z) tuples that are already
    booked in [start_utc, end_utc]. The bookable-slots endpoint always claims
    `isAvailable: true` regardless of bookings — must cross-reference here."""
    if not parent_ids:
        return set()

    def _iso_z(d: datetime) -> str:
        return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"

    out = client.get(
        "/bookings",
        params={
            "s": {
                "productId": {"$in": parent_ids},
                "startDate": {"$gte": _iso_z(start_utc)},
                "endDate":   {"$lte": _iso_z(end_utc)},
                "status": {"$ne": 2},  # exclude cancelled
            },
            "limit": 500,
        },
    )
    data = out.get("data", out) if isinstance(out, dict) else out
    booked: set[tuple[int, str]] = set()
    for b in data or []:
        pid = b.get("productId")
        sd = b.get("startDate")
        if pid is None or not sd:
            continue
        # Only count fully-booked slots (1-on-1 padel court reservations have
        # availableParticipantCount=0 when reserved).
        avail = b.get("availableParticipantCount")
        if avail is not None and avail > 0:
            continue
        booked.add((int(pid), sd))
    return booked


def get_product_names(client: BackboneClient, product_ids: set[int] | list[int]) -> dict[int, str]:
    """Bulk-fetch human-readable names for a set of product IDs (e.g. court names)."""
    ids = sorted(set(int(i) for i in product_ids))
    if not ids:
        return {}
    out = client.get("/products", params={"s": {"id": {"$in": ids}}})
    items = out.get("data", []) if isinstance(out, dict) else out
    names: dict[int, str] = {}
    for p in items or []:
        pid = p.get("id")
        if pid is None:
            continue
        for k in ("description_en", "description_nl", "name", "description"):
            if p.get(k):
                names[int(pid)] = p[k].strip()
                break
        else:
            names[int(pid)] = f"Court {pid}"
    return names


def find_slot_at_time(
    slots: list[Slot],
    hh_mm_local: str,
    tz_offset_hours: int | None = None,
    *,
    court: int | str | None = None,
    court_names: dict[int, str] | None = None,
) -> Slot | None:
    """Pick the slot that starts at `hh_mm_local` (Brussels time, DST-aware).

    `court` filters by court (e.g. 2 -> "Padel 2", or pass the full name).
    Requires `court_names` (resolve via `get_product_names`) to map IDs.
    Without `court`, returns the first slot at that time.
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Brussels")
    matches = [s for s in slots if s.start_dt.astimezone(tz).strftime("%H:%M") == hh_mm_local]
    if not matches:
        return None
    if court is None:
        return matches[0]
    if court_names is None:
        raise RuntimeError("court_names dict required when filtering by court")
    target = f"padel {court}".lower() if isinstance(court, int) else str(court).strip().lower()
    for s in matches:
        name = (court_names.get(s.bookable_product_id) or "").strip().lower()
        if name == target:
            return s
    return None


# --- the booking action ---

def _slot_params(slot: Slot, *, label: str | None = None) -> dict:
    """The shared `params` blob used by both /cart-items and /sales.
    `label` becomes the `primaryPurchaseMessage` (free-text annotation)."""
    return {
        "startDate": slot.start_iso,
        "endDate": slot.end_iso,
        "bookableProductId": slot.bookable_product_id,
        "bookableLinkedProductId": slot.bookable_linked_product_id,
        "invitedMemberEmails": [],
        "invitedGuests": [],
        "invitedOthers": [],
        "primaryPurchaseMessage": label,
        "secondaryPurchaseMessage": None,
        "totalParticipantInvitations": 0,
    }


def add_to_cart(client: BackboneClient, slot: Slot, member_id: int, *, label: str | None = None) -> dict:
    body = {
        "type": "product",
        "productId": slot.bookable_product_id,
        "amount": 1,
        "params": _slot_params(slot, label=label),
        "category": 1,
        "siteId": SITE_ID,
        "memberId": member_id,
    }
    return client.post("/cart-items", body)


def create_sale(client: BackboneClient, slot: Slot, member_id: int, *, label: str | None = None) -> dict:
    """Reserves the slot. Response includes the sale id and (after a follow-up) the booking id."""
    body = {
        "siteId": SITE_ID,
        "salesPersonId": SALES_PERSON_ID,
        "customerPersonId": member_id,
        "organizationId": None,
        "orderNumber": None,
        "posId": None,
        "saleItems": [
            {
                "productId": slot.bookable_linked_product_id,
                "subscriptionId": None,
                "courseId": None,
                "quantity": 1,
                "params": _slot_params(slot, label=label),
                "customerCategory": 1,
                "siteId": SITE_ID,
            }
        ],
        "date": None,
        "modificationDate": None,
    }
    return client.post("/sales", body)


def fetch_booking_id(client: BackboneClient, sale_id: int) -> int | None:
    """Look up the bookingId attached to a successful sale."""
    out = client.get(
        f"/sales/{sale_id}/sale-items",
        params={"join": "booking"},
    )
    items = out.get("data") if isinstance(out, dict) else out
    for it in items or []:
        if it.get("bookingId"):
            return int(it["bookingId"])
        b = it.get("booking")
        if b and b.get("id"):
            return int(b["id"])
    return None


@dataclass
class BookingResult:
    sale_id: int
    booking_id: int | None
    amount: float | None
    description: str
    slot: Slot
    paid: bool = False
    pay_method: str | None = None
    pay_error: str | None = None
    payment_url: str | None = None  # Worldline checkout link if CREDIT_CARD path


def book(
    client: BackboneClient,
    slot: Slot,
    *,
    member_id: int,
    auto_pay: bool = True,
    me: dict | None = None,
    label: str | None = None,
) -> BookingResult:
    """Cart → sale → payment (optional).

    If `auto_pay=True` and the account has a SEPA mandate, payment is created
    via PAY_METHOD_DIRECT_DEBIT (no Worldline, no 3DS). On failure the booking
    is still reserved — the caller should DM the user to pay manually.

    `label` is stored as `primaryPurchaseMessage` (free-text booking note like
    "for Sara & Tom"). Visible in the purchase history.
    """
    logger.info("Adding to cart: {}{}", slot.label(), f" [{label}]" if label else "")
    add_to_cart(client, slot, member_id, label=label)
    logger.info("Creating sale (this reserves the slot)...")
    sale = create_sale(client, slot, member_id, label=label)
    sale_id = sale.get("id") or sale.get("data", {}).get("id")
    if not sale_id:
        raise RuntimeError(f"No sale id in response: {sale!r}")
    amount = sale.get("total") or sale.get("data", {}).get("total")
    desc = sale.get("description") or sale.get("data", {}).get("description") or slot.label()
    booking_id = fetch_booking_id(client, sale_id)

    result = BookingResult(
        sale_id=int(sale_id),
        booking_id=booking_id,
        amount=amount,
        description=desc,
        slot=slot,
    )

    # NOTE: `paidFor: True` on a freshly-created booking is a 60-min
    # provisional hold flag, NOT confirmation of payment. The booking auto-
    # cancels at the 60-min mark without an actual /payments POST.
    # So always attempt auto_pay; only the /payments success means paid.
    if auto_pay and amount and not result.paid:
        try:
            from .payment import auto_pay as _auto_pay, build_payment_url, PayMethod
            if me is None:
                me = client.me()
            method, resp = _auto_pay(client, sale_id=int(sale_id), member_id=member_id, amount=float(amount), me=me)
            result.pay_method = method.name
            if method == PayMethod.CREDIT_CARD:
                url = build_payment_url(resp, me, float(amount))
                logger.info("Built payment URL: {}", url)
                result.payment_url = url
                # If a card is stored, drive the Worldline page headlessly.
                from .worldline_driver import load_card, pay_via_worldline
                card = load_card()
                if card and url:
                    logger.info("Driving Worldline checkout headlessly...")
                    drv = pay_via_worldline(payment_url=url, card=card, headless=True)
                    if drv.success:
                        result.paid = True
                        result.pay_method = "CREDIT_CARD (auto)"
                        logger.info("Worldline driver SUCCESS: {}", drv.final_url)
                    else:
                        result.pay_error = drv.error or "worldline driver failed"
                        logger.warning("Worldline driver FAILED: {}", drv.error)
                        # User still has the URL to click manually.
                else:
                    if not card:
                        logger.info("No card stored; surfacing payment URL only.")
                    result.paid = False
            else:
                result.paid = True
        except Exception as e:
            result.pay_error = str(e)
            logger.warning("auto_pay failed (booking still reserved): {}", e)

    return result
