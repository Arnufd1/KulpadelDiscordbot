from copy import deepcopy

import httpx
import pytest

from padelbot import booking, cart, payment
from padelbot.booking import Slot


SLOT = Slot(1, 2, "2030-10-08T17:30:00Z", "2030-10-08T18:59:00Z", 8, {})
ITEM = {"id": 10, "memberId": 42, "type": "product", "productId": 1,
        "amount": 1, "category": 1, "params": {
            "bookableProductId": 1, "bookableLinkedProductId": 2,
            "startDate": "2030-10-08T17:30:00.000Z", "endDate": "2030-10-08T18:59:00.000Z"}}


class Client:
    def __init__(self, items=None):
        self.items = deepcopy(items or [])
        self.posts, self.deletes = [], []
        self.on_post = None
        self.on_delete = None
    def get(self, path, params=None):
        if path == "/cart-items":
            return {"data": deepcopy(self.items), "total": len(self.items)}
        if path == "/public/tags": return [{"id": 72, "description": "Padel"}]
        if path == "/tags/query/availableActivityTagsAndProductIds": return {"productIdsByTagId": {"72": [2, 3]}}
        if path == "/sales/12/sale-items": return {"data": [{"bookingId": 123}]}
        raise AssertionError(path)
    def post(self, path, body):
        self.posts.append((path, deepcopy(body)))
        if self.on_post: return self.on_post(path, body)
        if path == "/sales": return {"id": 12, "total": 8}
        item = {**deepcopy(body), "id": 20}
        self.items.append(item)
        return deepcopy(item)
    def delete(self, path):
        self.deletes.append(path)
        if self.on_delete: return self.on_delete(path)
        iid = int(path.rsplit("/", 1)[1])
        self.items = [i for i in self.items if i["id"] != iid]
        return {}


def conflict_error(message="this item is already in your cart or not allow simultaneous booking"):
    request = httpx.Request("POST", "https://example.invalid/cart-items")
    response = httpx.Response(403, request=request, json={"message": message})
    return httpx.HTTPStatusError(message, request=request, response=response)


def test_exact_owned_cart_item_is_reused_without_a_duplicate_post():
    client = Client([ITEM])
    result = booking.add_to_cart(client, SLOT, 42)
    assert result["id"] == 10
    assert client.posts == [] and client.deletes == []


def test_existing_item_continues_to_one_sale_and_only_matching_cart_is_cleaned():
    unrelated = {"id": 99, "memberId": 42, "type": "subscription", "amount": 1}
    foreign = dict(ITEM, id=98, memberId=43)
    client = Client([ITEM, unrelated, foreign])
    result = booking.book(client, SLOT, member_id=42, me={"category": "1"}, auto_pay=False)
    assert result.sale_id == 12
    assert [p for p, _ in client.posts] == ["/sales"]
    assert client.deletes == ["/cart-items/10"]
    assert {i["id"] for i in client.items} == {98, 99}


@pytest.mark.parametrize("changes", [{"category": 2}, {"amount": 2},
    {"params": {**ITEM["params"], "invitedGuests": [{"name": "Test"}]}}])
def test_incompatible_item_for_selected_slot_is_replaced_after_confirmation(changes):
    client = Client([{**ITEM, **changes}])
    result = booking.add_to_cart(client, SLOT, 42)
    assert client.deletes == ["/cart-items/10"]
    assert len(client.posts) == 1 and result["category"] == 1 and result["amount"] == 1


def test_conflicting_other_padel_court_is_removed_but_other_purchases_are_kept():
    conflict = deepcopy(ITEM)
    conflict["params"].update(bookableProductId=4, bookableLinkedProductId=3,
                              startDate="2030-10-08T17:00:00Z", endDate="2030-10-08T18:29:00Z")
    other = deepcopy(conflict)
    other["id"] = 11
    other["params"]["bookableLinkedProductId"] = 999
    client = Client([conflict, other])
    booking.add_to_cart(client, SLOT, 42)
    assert client.deletes == ["/cart-items/10"]
    assert any(i["id"] == 11 for i in client.items)


def test_cart_item_bound_to_existing_booking_is_never_deleted():
    existing = deepcopy(ITEM)
    existing["params"]["bookingId"] = 123
    client = Client([existing])
    def blocked(*args): raise conflict_error()
    client.on_post = blocked
    with pytest.raises(RuntimeError, match="overlappende"):
        booking.add_to_cart(client, SLOT, 42)
    assert client.deletes == [] and len(client.posts) == 1


def test_conflict_race_reuses_item_that_appeared_after_initial_read():
    client = Client()
    def raced(path, body):
        client.items.append(deepcopy(ITEM))
        raise conflict_error()
    client.on_post = raced
    assert booking.add_to_cart(client, SLOT, 42)["id"] == 10
    assert len(client.posts) == 1


def test_known_conflict_retries_once_only_after_an_owned_item_was_removed():
    client = Client()
    def raced(path, body):
        if len(client.posts) == 1:
            client.items.append({**deepcopy(ITEM), "category": 2})
            raise conflict_error()
        item = {**deepcopy(body), "id": 20}
        client.items.append(item)
        return item
    client.on_post = raced
    assert booking.add_to_cart(client, SLOT, 42)["id"] == 20
    assert len(client.posts) == 2 and client.deletes == ["/cart-items/10"]


def test_real_simultaneous_booking_without_cart_evidence_is_not_blindly_retried():
    client = Client()
    def blocked(*args): raise conflict_error()
    client.on_post = blocked
    with pytest.raises(RuntimeError, match="geen dubbele"):
        booking.add_to_cart(client, SLOT, 42)
    assert len(client.posts) == 1 and client.deletes == []


@pytest.mark.parametrize("created", [False, True])
def test_lost_cart_response_is_reconciled_using_reads_without_repeating_post(created):
    client = Client()
    def lost(*args):
        if created: client.items.append(deepcopy(ITEM))
        raise httpx.ReadTimeout("unknown", request=httpx.Request("POST", "https://example.invalid/cart-items"))
    client.on_post = lost
    if created: assert booking.add_to_cart(client, SLOT, 42)["id"] == 10
    else:
        with pytest.raises(cart.CartStateUnknownError): booking.add_to_cart(client, SLOT, 42)
    assert len(client.posts) == 1


def test_foreign_cart_item_is_not_reused_or_removed():
    client = Client([dict(ITEM, memberId=43)])
    item = booking.add_to_cart(client, SLOT, 42)
    assert item["memberId"] == 42 and item["id"] != 10
    assert client.deletes == []


def test_failed_cleanup_does_not_skip_payment_for_a_created_reservation(monkeypatch):
    client = Client([ITEM])
    def unavailable(*args): raise httpx.ConnectError("network unavailable")
    client.on_delete = unavailable
    payments = []
    monkeypatch.setattr(payment, "auto_pay", lambda *a, **kw: payments.append(kw["sale_id"]) or (payment.PayMethod.ACCOUNT, {}))
    monkeypatch.setattr(payment, "wait_for_payment_confirmation", lambda *a, **kw: True)
    result = booking.book(client, SLOT, member_id=42, me={"category": "1"})
    assert result.paid and payments == [12]
    assert [p for p, _ in client.posts] == ["/sales"]


def test_cart_changed_to_another_slot_is_not_deleted(monkeypatch):
    client = Client([ITEM])
    changed = deepcopy(ITEM)
    changed["params"]["startDate"] = "2030-10-09T17:30:00Z"
    client.items = [changed]
    with pytest.raises(cart.CartStateUnknownError, match="gewijzigd"):
        cart.remove_own_item(client, ITEM, 42)
    assert client.deletes == []
