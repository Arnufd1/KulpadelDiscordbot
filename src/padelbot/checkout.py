"""Read-only booking preview and automatic-payment prerequisites."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import time

from .booking import Slot
from .card_store import validate_card
from .payment import has_direct_debit_mandate
from .worldline_driver import load_card, _detect_card_brand


@dataclass(frozen=True)
class BookingQuote:
    slot: Slot
    member_id: int
    amount: Decimal
    expires_at: float

    @property
    def amount_label(self) -> str:
        return f"€{self.amount:.2f}".replace(".", ",")


def slot_price(client, slot: Slot, me: dict) -> Decimal:
    """Match USC ProductService's member-category price for a padel slot.

    No invited guests, quantity changes or hourly rental multipliers are used
    for these fixed padel products. Unsupported discount rules fail closed.
    The final sale total is checked again before starting payment.
    """
    linked = client.get(f"/products/{slot.bookable_linked_product_id}")
    parent = client.get(f"/products/{slot.bookable_product_id}")
    if linked.get("id") != slot.bookable_linked_product_id or parent.get("id") != slot.bookable_product_id:
        raise RuntimeError("De prijs van dit veld kon niet bevestigd worden.")
    if linked.get("linkedSubsPrice") is not None:
        raise RuntimeError("Dit slot gebruikt een afwijkende abonnementsprijs. Controleer die in KU Leuven Sport.")
    try:
        category = int(me.get("category") or 0)
        value = linked.get(f"category{category}Price") if category > 0 else None
        if value is None and linked.get("price") is None:
            value = parent.get(f"category{category}Price") if category > 0 else None
            if value is None:
                value = parent.get("price")
        if value is None:
            value = linked.get("price")
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount == 9999:
            raise ValueError()
        return amount.quantize(Decimal("0.01"))
    except (ValueError, TypeError, InvalidOperation):
        raise RuntimeError("Geen geldige boekingsprijs gevonden; er is niets geboekt.") from None


def automatic_payment_ready(me: dict, amount: Decimal) -> None:
    """Stop before reserving if no automatic payment method is ready."""
    try:
        balance = Decimal(str(me.get("balance") or 0))
        if balance.is_finite() and balance >= amount:
            return
        if has_direct_debit_mandate(me) and int(me.get("directDebitSpec") or 0) > 0:
            return
    except (ValueError, TypeError, InvalidOperation):
        pass
    try:
        card = load_card()
        if card is None:
            raise RuntimeError("Stel eerst je betaalkaart lokaal in; er is nog niets geboekt.")
        validate_card(card)
        if not _detect_card_brand(card.number):
            raise RuntimeError("Dit kaarttype wordt nog niet ondersteund voor automatisch betalen.")
    except ValueError as error:
        raise RuntimeError(str(error)) from None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
    except Exception:
        raise RuntimeError("De automatische betaalbrowser is niet beschikbaar; er is nog niets geboekt.") from None


def make_quote(client, slot: Slot, me: dict) -> BookingQuote:
    amount = slot_price(client, slot, me)
    automatic_payment_ready(me, amount)
    return BookingQuote(slot, int(me["id"]), amount, time.monotonic() + 120)
