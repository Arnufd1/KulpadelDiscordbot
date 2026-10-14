from types import SimpleNamespace

import pytest

from padelbot import booking_manager


class Client:
    def __init__(self, *, extra_item=False, owner=42, response=None, error=None):
        self.extra_item, self.owner, self.response, self.error = extra_item, owner, response, error
        self.writes = []
    def get(self, path, params=None):
        if path == "/sales/12": return {"id": 12, "customerPersonId": self.owner}
        if path == "/sales/12/sale-items":
            return {"data": [{"bookingId": 123}] + ([{"bookingId": 124}] if self.extra_item else [])}
        raise AssertionError(path)
    def patch(self, path, body):
        self.writes.append((path, body))
        if self.error: raise self.error
        return self.response or {"id": 12, "statusCode": 2}


def own_booking(monkeypatch, *, remains=False, raw=None):
    calls = []
    def listing(*args, **kwargs):
        calls.append(kwargs)
        return [SimpleNamespace(id=123, sale_id=12, raw=raw or {},
                start_iso="2030-10-08T17:30:00Z", end_iso="2030-10-08T18:59:00Z")] if len(calls) == 1 or remains else []
    monkeypatch.setattr(booking_manager, "list_my_bookings", listing)
    return calls


@pytest.mark.parametrize("response", [{"id": 12, "statusCode": 2}, {"id": 12, "statusCode": 0, "creditSales": [{"id": 13}]}])
def test_cancel_own_single_booking_uses_frontend_sale_endpoint_and_verifies(monkeypatch, response):
    calls = own_booking(monkeypatch)
    client = Client(response=response)
    booking_manager.cancel_booking(client, booking_id=123, member_id=42)
    assert client.writes == [("/sales/12", {"statusCode": 2})]
    assert len(calls) == 2 and all(c["member_id"] == 42 for c in calls)


@pytest.mark.parametrize("kwargs", [{"extra_item": True}, {"owner": 43}])
def test_combined_purchase_and_other_member_are_not_cancelled(monkeypatch, kwargs):
    own_booking(monkeypatch)
    client = Client(**kwargs)
    with pytest.raises(RuntimeError): booking_manager.cancel_booking(client, booking_id=123, member_id=42)
    assert client.writes == []


def test_booking_not_in_own_upcoming_list_is_not_cancelled(monkeypatch):
    monkeypatch.setattr(booking_manager, "list_my_bookings", lambda *a, **kw: [])
    client = Client()
    with pytest.raises(RuntimeError): booking_manager.cancel_booking(client, booking_id=123, member_id=42)
    assert client.writes == []


@pytest.mark.parametrize("kwargs", [{"error": TimeoutError("uncertain")}, {"response": {"id": 12, "statusCode": 1}}])
def test_uncertain_cancel_does_not_try_another_write(monkeypatch, kwargs):
    own_booking(monkeypatch)
    client = Client(**kwargs)
    with pytest.raises((TimeoutError, RuntimeError)):
        booking_manager.cancel_booking(client, booking_id=123, member_id=42)
    assert len(client.writes) == 1


def test_still_visible_reservation_does_not_claim_confirmed_cancellation(monkeypatch):
    own_booking(monkeypatch, remains=True)
    with pytest.raises(RuntimeError, match="staat nog"):
        booking_manager.cancel_booking(Client(), booking_id=123, member_id=42)


def test_confirmed_cancellation_cleans_only_the_cancelled_slot_cart(monkeypatch):
    from padelbot import cart
    calls = own_booking(monkeypatch, raw={"productId": 1, "linkedProductId": 2})
    cleaned = []
    def cleanup(client, slot, member_id):
        assert len(calls) == 2
        cleaned.append((slot.bookable_product_id, slot.bookable_linked_product_id,
                        slot.start_iso, slot.end_iso, member_id))
    monkeypatch.setattr(cart, "cleanup_slot_cart", cleanup)
    booking_manager.cancel_booking(Client(), booking_id=123, member_id=42)
    assert cleaned == [(1, 2, "2030-10-08T17:30:00Z", "2030-10-08T18:59:00Z", 42)]


def test_cleanup_failure_keeps_a_confirmed_cancellation_confirmed(monkeypatch):
    from padelbot import cart
    own_booking(monkeypatch, raw={"productId": 1, "linkedProductId": 2})
    def failed_cleanup(*args):
        raise TimeoutError("cart cleanup delayed")
    monkeypatch.setattr(cart, "cleanup_slot_cart", failed_cleanup)
    assert booking_manager.cancel_booking(Client(), booking_id=123, member_id=42)["statusCode"] == 2
