from copy import deepcopy
from datetime import datetime, timezone, timedelta

import pytest

from padelbot import booking


NOW = datetime(2030, 10, 8, 10, tzinfo=timezone.utc)
ROW = {"bookableProductId": 4385, "linkedProductId": 4610,
       "startDate": "2030-10-08T17:30:00.000Z", "endDate": "2030-10-08T18:59:00.000Z",
       "isAvailable": True}
OCCUPIED = {"productId": 4385, "startDate": ROW["startDate"], "endDate": ROW["endDate"],
            "status": 0, "availableParticipantCount": 0}


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class Client:
    def __init__(self, rows=None, bookings=None, links=None):
        self.rows = deepcopy(rows if rows is not None else [ROW])
        self.bookings = deepcopy(bookings or [])
        self.links = links or []
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, deepcopy(params)))
        if path == "/public/tags": return [{"id": 72, "description": "Padel"}]
        if path == "/tags/query/availableActivityTagsAndProductIds": return {"productIdsByTagId": {"72": [4610]}}
        if path == "/products/bookable-slots": return {"data": self.rows}
        if path == "/products/dependant-product-links": return {"data": self.links}
        if path == "/bookings":
            start = (params["page"] - 1) * params["limit"]
            return {"data": self.bookings[start:start + params["limit"]], "total": len(self.bookings)}
        raise AssertionError(path)


@pytest.fixture(params=["single", "window"])
def discover(request, monkeypatch):
    monkeypatch.setattr(booking, "datetime", FrozenDatetime)
    def run(client):
        if request.param == "single":
            return booking.find_slots(client, target_date=NOW)
        return booking.find_slots_window(client, start_date=NOW, days=1)
    return run


def test_reserved_court_is_excluded_even_when_api_says_available(discover):
    other = dict(ROW, bookableProductId=4386)
    slots = discover(Client(rows=[ROW, other], bookings=[OCCUPIED]))
    assert [s.bookable_product_id for s in slots] == [4386]


def test_shared_block_excludes_overlapping_slots_on_both_courts(discover):
    block = dict(OCCUPIED, productId=4647, startDate="2030-10-08T17:00:00Z", endDate="2030-10-08T18:29:00Z")
    client = Client(rows=[ROW, dict(ROW, bookableProductId=4386)], bookings=[block],
                    links=[{"parentId": 4385, "childId": 4647}, {"parentId": 4386, "childId": 4647}])
    assert discover(client) == []
    query = next(params for path, params in client.calls if path == "/bookings")
    assert set(query["s"]["productId"]["$in"]) == {4385, 4386, 4647}


def test_same_court_overlapping_reservation_with_different_start_is_excluded(discover):
    assert discover(Client(bookings=[dict(OCCUPIED, startDate="2030-10-08T17:00:00Z")])) == []


@pytest.mark.parametrize("changes", [{"status": 2}, {"availableParticipantCount": 3}])
def test_cancelled_bookings_and_group_sessions_with_free_places_do_not_block(discover, changes):
    assert len(discover(Client(bookings=[dict(OCCUPIED, **changes)]))) == 1


def test_equivalent_utc_timestamps_match(discover):
    assert discover(Client(bookings=[dict(OCCUPIED, startDate="2030-10-08T19:30:00+02:00")])) == []


def test_non_overlapping_booking_does_not_block(discover):
    previous = dict(OCCUPIED, startDate="2030-10-08T16:00:00Z", endDate="2030-10-08T17:29:00Z")
    assert len(discover(Client(bookings=[previous]))) == 1


def test_busy_booking_beyond_first_page_is_also_excluded(discover):
    client = Client(bookings=[dict(OCCUPIED, productId=999)] * 500 + [OCCUPIED])
    assert discover(client) == []
    assert [p["page"] for path, p in client.calls if path == "/bookings"] == [1, 2]


@pytest.mark.parametrize("row", [dict(ROW, isAvailable=False),
    dict(ROW, startDate="2030-10-08T09:00:00Z", endDate="2030-10-08T09:59:00Z"),
    dict(ROW, startDate="2030-10-15T17:30:00Z", endDate="2030-10-15T18:59:00Z")])
def test_unavailable_past_and_unopened_slots_are_excluded(discover, row):
    assert discover(Client(rows=[row])) == []


def test_failed_occupancy_fetch_does_not_report_slots_as_free(discover):
    class Failing(Client):
        def get(self, path, params=None):
            if path == "/bookings": raise RuntimeError("API unavailable")
            return super().get(path, params)
    with pytest.raises(RuntimeError, match="API unavailable"):
        discover(Failing())


def test_exact_duplicate_slots_with_equivalent_offsets_are_deduplicated(discover):
    equivalent = dict(ROW, startDate="2030-10-08T19:30:00+02:00",
                      endDate="2030-10-08T20:59:00+02:00")
    slots = discover(Client(rows=[ROW, deepcopy(ROW), equivalent]))
    assert len(slots) == 1
    assert slots[0].start_iso == ROW["startDate"]


@pytest.mark.parametrize("changes", [
    {"endDate": "2030-10-08T19:29:00Z"},
    {"linkedProductId": 4611},
    {"bookableProductId": 4386},
])
def test_same_start_preserves_different_durations_and_court_identities(discover, changes):
    distinct = dict(ROW, **changes)
    slots = discover(Client(rows=[ROW, distinct, deepcopy(distinct)]))
    assert len(slots) == 2
    assert [(s.start_iso, s.end_iso, s.bookable_product_id, s.bookable_linked_product_id)
            for s in slots] == [
        (row["startDate"], row["endDate"], row["bookableProductId"], row["linkedProductId"])
        for row in [ROW, distinct]]


def test_slot_datetime_respects_iso_offset():
    slot = booking.Slot(1, 2, "2030-10-08T19:30:00+02:00", "2030-10-08T21:00:00+02:00", None, {})
    assert slot.start_dt == datetime(2030, 10, 8, 17, 30, tzinfo=timezone.utc)
