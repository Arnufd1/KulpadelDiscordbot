from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
import asyncio
import time

import pytest

from padelbot import checkout, discord_ui as ui, booking, payment
from padelbot.booking import Slot


SLOT = Slot(4385, 4610, "2030-10-08T17:30:00Z", "2030-10-08T18:59:00Z", None, {})
PRODUCT = {"id": 4610, "price": 24, "category1Price": 8, "category2Price": 16, "linkedSubsPrice": None}
MEMBER = {"id": 42, "category": "1", "balance": 0}


class Client:
    def __init__(self, linked=None, parent=None, me=None):
        self.linked = deepcopy(linked or PRODUCT)
        self.parent = deepcopy(parent or {"id": 4385, "price": 24})
        self.member = me or MEMBER
        self.calls = []
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def me(self): return self.member
    def get(self, path, params=None):
        self.calls.append(path)
        if path == "/products/4610": return self.linked
        if path == "/products/4385": return self.parent
        raise AssertionError(path)
    def post(self, *args, **kwargs): pytest.fail("Preview must not write")


@pytest.mark.parametrize("category,price", [("1", "8.00"), ("2", "16.00"), ("0", "24.00")])
def test_preview_uses_member_price_instead_of_generic_product_price(category, price):
    client = Client()
    assert checkout.slot_price(client, SLOT, dict(MEMBER, category=category)) == Decimal(price)
    assert client.calls == ["/products/4610", "/products/4385"]


def test_missing_linked_price_falls_back_to_parent_category():
    client = Client(linked=dict(PRODUCT, category1Price=None, price=None),
                    parent={"id": 4385, "category1Price": 9, "price": 24})
    assert checkout.slot_price(client, SLOT, MEMBER) == Decimal("9.00")


@pytest.mark.parametrize("fields", [{"category1Price": "NaN"}, {"category1Price": -1},
    {"category1Price": 9999}, {"id": 999}, {"linkedSubsPrice": 4}])
def test_unsupported_or_invalid_price_cannot_be_confirmed(fields):
    with pytest.raises(RuntimeError): checkout.slot_price(Client(linked=dict(PRODUCT, **fields)), SLOT, MEMBER)


def test_make_quote_is_read_only_and_has_short_expiry(monkeypatch):
    checks = []
    monkeypatch.setattr(checkout, "automatic_payment_ready", lambda me, amount: checks.append(amount))
    before = time.monotonic()
    quote = checkout.make_quote(Client(), SLOT, MEMBER)
    assert quote.amount == Decimal("8.00") and quote.member_id == 42
    assert before + 120 <= quote.expires_at <= time.monotonic() + 120
    assert checks == [Decimal("8.00")]


def test_missing_card_blocks_automatic_checkout_before_booking(monkeypatch):
    monkeypatch.setattr(checkout, "load_card", lambda: None)
    with pytest.raises(RuntimeError, match="betaalkaart"):
        checkout.automatic_payment_ready(MEMBER, Decimal("8"))


@pytest.mark.parametrize("me", [dict(MEMBER, balance=8), dict(MEMBER, directDebitMandate="test", directDebitSpec=1)])
def test_balance_and_direct_debit_do_not_require_a_card(monkeypatch, me):
    monkeypatch.setattr(checkout, "load_card", lambda: pytest.fail("Card should not be required"))
    checkout.automatic_payment_ready(me, Decimal("8"))


def setup_selection(monkeypatch, client=None):
    client = client or Client()
    monkeypatch.setattr(ui, "BackboneClient", lambda: client)
    monkeypatch.setattr(ui, "find_slots_window", lambda *a, **kw: [SLOT])
    monkeypatch.setattr(ui, "list_my_bookings", lambda *a, **kw: [])
    monkeypatch.setattr(ui, "slot_price", lambda *a, **kw: Decimal("8.00"))
    monkeypatch.setattr(ui, "automatic_payment_ready", lambda *a, **kw: None)
    chosen = ui.DaySlotsView(42, SLOT.start_dt, [SLOT], {4385: "Padel 4"}, time=ui.local_time(SLOT), court=4385)
    quote = checkout.BookingQuote(SLOT, 42, Decimal("8.00"), time.monotonic() + 120)
    return chosen, quote


def test_expired_confirmation_never_calls_booking_api(monkeypatch):
    async def run():
        chosen, quote = setup_selection(monkeypatch)
        monkeypatch.setattr(ui, "BackboneClient", lambda: pytest.fail("Expired confirmation must not access API"))
        with pytest.raises(RuntimeError, match="verlopen"):
            chosen.reserve_now(replace(quote, expires_at=time.monotonic() - 1))
    asyncio.run(run())


@pytest.mark.parametrize("change", ["account", "price", "duration"])
def test_changed_details_stop_before_reservation_and_payment(monkeypatch, change):
    async def run():
        client = Client(me=dict(MEMBER, id=43)) if change == "account" else Client()
        chosen, quote = setup_selection(monkeypatch, client)
        if change == "price": monkeypatch.setattr(ui, "slot_price", lambda *a: Decimal("16"))
        if change == "duration": monkeypatch.setattr(ui, "find_slots_window", lambda *a, **kw: [replace(SLOT, end_iso="2030-10-08T20:00:00Z")])
        monkeypatch.setattr(ui, "book", lambda *a, **kw: pytest.fail("Changed details must not be booked"))
        with pytest.raises(RuntimeError): chosen.reserve_now(quote)
    asyncio.run(run())


def test_overlapping_own_booking_is_rejected_even_with_different_start(monkeypatch):
    async def run():
        chosen, quote = setup_selection(monkeypatch)
        monkeypatch.setattr(ui, "list_my_bookings", lambda *a, **kw: [SimpleNamespace(start_iso="2030-10-08T17:00:00Z", end_iso="2030-10-08T18:29:00Z")])
        monkeypatch.setattr(ui, "book", lambda *a, **kw: pytest.fail("Overlapping own booking"))
        with pytest.raises(RuntimeError, match="overlapt"): chosen.reserve_now(quote)
    asyncio.run(run())


def test_confirmed_amount_is_passed_to_actual_booking(monkeypatch):
    async def run():
        chosen, quote = setup_selection(monkeypatch)
        calls = []
        def reserve(client, slot, **kwargs): calls.append(kwargs); return SimpleNamespace(paid=True)
        monkeypatch.setattr(ui, "book", reserve)
        chosen.reserve_now(quote)
        assert len(calls) == 1
        assert calls[0]["expected_amount"] == Decimal("8.00") and calls[0]["member_id"] == 42
    asyncio.run(run())


def test_sale_total_race_cancels_only_the_new_unpaid_reservation(monkeypatch):
    async def run():
        chosen, quote = setup_selection(monkeypatch)
        result = booking.BookingResult(12, 123, 16, "test", SLOT)
        def changed(*a, **kw): raise booking.BookingPriceChangedError(result)
        cancellations = []
        monkeypatch.setattr(ui, "book", changed)
        monkeypatch.setattr(ui, "cancel_booking", lambda *a, **kw: cancellations.append(kw))
        with pytest.raises(RuntimeError, match="geannuleerd"): chosen.reserve_now(quote)
        assert cancellations == [{"booking_id": 123, "member_id": 42}]
    asyncio.run(run())


def test_actual_sale_amount_must_match_confirmed_quote_before_any_payment(monkeypatch):
    categories = []
    monkeypatch.setattr(booking, "add_to_cart", lambda *a, **kw: categories.append(kw["customer_category"]))
    monkeypatch.setattr(booking, "create_sale", lambda *a, **kw: {"id": 12, "total": 16})
    monkeypatch.setattr(booking, "fetch_booking_id", lambda *a: 123)
    monkeypatch.setattr(payment, "auto_pay", lambda *a, **kw: pytest.fail("Unapproved amount must not be paid"))
    with pytest.raises(booking.BookingPriceChangedError) as error:
        booking.book(Client(), SLOT, member_id=42, me=dict(MEMBER, category="2"), expected_amount=Decimal("8"))
    assert error.value.result.sale_id == 12 and error.value.result.booking_id == 123
    assert categories == [2]


@pytest.mark.parametrize("amount", [Decimal("NaN"), Decimal("-1"), Decimal("0")])
def test_invalid_confirmed_amount_never_writes_cart(monkeypatch, amount):
    monkeypatch.setattr(booking, "add_to_cart", lambda *a, **kw: pytest.fail("Invalid amount must not reserve"))
    with pytest.raises(ValueError): booking.book(Client(), SLOT, member_id=42, me=MEMBER, expected_amount=amount)
