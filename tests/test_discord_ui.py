import asyncio
from datetime import timedelta
from decimal import Decimal
import time
from types import SimpleNamespace

import discord
import pytest

from padelbot import discord_ui as ui
from padelbot.booking import Slot
from padelbot.config import settings
from padelbot.rules_store import RulesStore


OWNER = 42


def interaction(user=OWNER):
    edits, replies, defers = [], [], []
    async def edit(**kwargs): edits.append(kwargs)
    async def send(message, **kwargs): replies.append(message)
    async def defer(**kwargs): defers.append(kwargs)
    return SimpleNamespace(user=SimpleNamespace(id=user),
        response=SimpleNamespace(edit_message=edit, send_message=send, defer=defer),
        followup=SimpleNamespace(send=send), edit_original_response=edit,
        client=SimpleNamespace(_booking_lock=asyncio.Lock()), edits=edits, replies=replies, defers=defers)


def slots_for_day(count=30):
    base = ui.today().replace(hour=6)
    return [Slot(court, 4610, (base + timedelta(minutes=30 * i)).isoformat(),
                 (base + timedelta(minutes=30 * i + 89)).isoformat(), None, {})
            for i in range(count) for court in (4385, 4386)]


def test_all_times_and_courts_are_reachable_without_truncation(monkeypatch):
    monkeypatch.setattr(ui.DaySlotsView, "prepare_confirmation", quote_for)
    async def run():
        slots = slots_for_day()
        view = ui.DaySlotsView(OWNER, ui.today(), slots, {4385: "Padel 4", 4386: "Padel 5"})
        assert view.pages == 2
        assert len(view.children[0].options) == 25
        it = interaction()
        await view.next_times(it)
        second = it.edits[-1]["view"]
        assert len(second.children[0].options) == 5
        second.children[0]._values = [second.children[0].options[-1].value]
        await second.children[0].callback(it)
        time_view = it.edits[-1]["view"]
        assert len(time_view.children[1].options) == 2
        time_view.children[1]._values = ["4386"]
        await time_view.children[1].callback(it)
        final = it.edits[-1]["view"]
        assert isinstance(final, ui.ConfirmBookingView)
        assert final.selection.court == 4386
        assert sum(getattr(c, "label", "").startswith("Bevestigen en betalen") for c in final.children) == 1
        assert len(final.to_components()) <= 5
        assert len(final.embed().description) <= 4096
    asyncio.run(run())


def test_empty_day_keeps_navigation_and_refresh():
    async def run():
        view = ui.DaySlotsView(OWNER, ui.today(), [], {})
        assert "Geen beschikbare" in view.embed().description
        assert [c.label for c in view.children] == ["Vorige dag", "Volgende dag", "Vernieuwen", "Deel met groep"]
        assert view.children[0].disabled
    asyncio.run(run())


def test_date_navigation_refetches_actual_requested_day(monkeypatch):
    requests = []
    def fetch(target): requests.append(target); return [], {}
    monkeypatch.setattr(ui, "fetch_day", fetch)
    async def run():
        target = ui.today() + timedelta(days=6)
        view = ui.DaySlotsView(OWNER, target, [], {})
        it = interaction()
        await view.next_day(it)
        assert requests == [target + timedelta(days=1)]
        assert it.edits[-1]["view"].target == requests[0]
    asyncio.run(run())


def test_stale_slot_is_checked_again_before_any_booking(monkeypatch):
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def me(self): return {"id": 99}
    monkeypatch.setattr(ui, "BackboneClient", Client)
    monkeypatch.setattr(ui, "find_slots_window", lambda *a, **kw: [])
    monkeypatch.setattr(ui, "book", lambda *a, **kw: pytest.fail("Occupied slot must not be booked"))
    async def run():
        view = ui.DaySlotsView(OWNER, ui.today(), slots_for_day(1), {4385: "Padel 4", 4386: "Padel 5"}, time="06:00", court=4385)
        with pytest.raises(RuntimeError, match="intussen bezet"):
            view.prepare_confirmation()
    asyncio.run(run())


def quote_for(selection):
    slot = next(s for s in selection.slots if s.bookable_product_id == selection.court
                and s.bookable_linked_product_id == selection.linked_court
                and s.start_iso == selection.selected_group['start']
                and s.end_iso == selection.selected_group['end'])
    return ui.BookingQuote(slot, OWNER, Decimal("8.00"), time.monotonic() + 120)


def selection():
    return ui.DaySlotsView(OWNER, ui.today(), slots_for_day(1), {4385: "Padel 4", 4386: "Padel 5"}, time="06:00", court=4385)


def test_opening_confirmation_makes_no_booking_and_repeated_clicks_do_not_duplicate(monkeypatch):
    quotes = []
    monkeypatch.setattr(ui.DaySlotsView, "prepare_confirmation", lambda self: quotes.append(True) or quote_for(self))
    monkeypatch.setattr(ui.DaySlotsView, "reserve_now", lambda *a, **kw: pytest.fail("No confirmation was given"))
    async def run():
        view = selection()
        it = interaction()
        await asyncio.gather(view.reserve(it), view.reserve(it))
        assert quotes == [True]
        assert isinstance(it.edits[-1]["view"], ui.ConfirmBookingView)
        assert "8,00" in it.edits[-1]["embed"].description
        assert len(it.defers) == 1
    asyncio.run(run())


def test_repeated_confirmation_clicks_only_write_once(monkeypatch):
    calls = []
    monkeypatch.setattr(ui.DaySlotsView, "reserve_now", lambda self, quote, **kwargs: calls.append(quote.amount) or SimpleNamespace(paid=True))
    monkeypatch.setattr(ui, "payment_message", lambda result: "Betaling bevestigd")
    async def run():
        chosen = selection()
        view = ui.ConfirmBookingView(chosen, quote_for(chosen))
        it = interaction()
        await asyncio.gather(view.confirm(it), view.confirm(it))
        assert calls == [Decimal("8.00")]
        assert len(it.defers) == 1
        assert it.edits[-1]["view"] is None
        assert "Gereserveerd en betaald" in it.edits[-1]["content"]
    asyncio.run(run())


def test_back_from_confirmation_does_not_book(monkeypatch):
    monkeypatch.setattr(ui.DaySlotsView, "reserve_now", lambda *a, **kw: pytest.fail("Back is not consent"))
    async def run():
        chosen = selection()
        view = ui.ConfirmBookingView(chosen, quote_for(chosen))
        it = interaction()
        await view.back(it)
        assert isinstance(it.edits[-1]["view"], ui.DaySlotsView)
        assert view.busy
    asyncio.run(run())


def test_bank_verification_notice_appears_while_payment_is_running(monkeypatch):
    def reserve(self, quote, *, on_3ds_challenge):
        on_3ds_challenge()
        return SimpleNamespace(paid=True)
    monkeypatch.setattr(ui.DaySlotsView, "reserve_now", reserve)
    monkeypatch.setattr(ui, "payment_message", lambda result: "Betaling bevestigd")
    async def run():
        chosen = selection()
        view = ui.ConfirmBookingView(chosen, quote_for(chosen))
        it = interaction()
        await view.confirm(it)
        assert len(it.edits) == 3
        assert "bankcontrole" in it.edits[1]["content"]
        assert "Betaling bevestigd" in it.edits[2]["content"]
    asyncio.run(run())


@pytest.mark.parametrize("panel", ["slots", "bookings", "autobook", "confirmation"])
def test_non_owner_cannot_use_any_panel(tmp_path, panel):
    async def run():
        if panel == "slots": view = ui.DaySlotsView(OWNER, ui.today(), [], {})
        elif panel == "bookings": view = ui.BookingsView(OWNER, [], {})
        elif panel == "confirmation":
            chosen = selection()
            view = ui.ConfirmBookingView(chosen, quote_for(chosen))
        else: view = ui.AutoBookView(OWNER, RulesStore(tmp_path / "rules.sqlite3"))
        it = interaction(user=43)
        assert await view.interaction_check(it) is False
        assert len(it.replies) == 1
    asyncio.run(run())


@pytest.mark.parametrize("time", ["99:99", "24:00", "20:60", "ab:cd"])
def test_invalid_autobook_time_does_not_save_a_rule(tmp_path, time):
    async def run():
        store = RulesStore(tmp_path / "rules.sqlite3")
        modal = ui.AutoBookModal(OWNER, store)
        modal.weekday._values, modal.start_time._values, modal.end_time._values = ["vrijdag"], [time], ["21:00"]
        await modal.on_submit(interaction())
        assert store.list_rules() == []
    asyncio.run(run())


def test_autobook_modal_saves_once_and_can_pause_resume_remove(tmp_path):
    async def run():
        store = RulesStore(tmp_path / "rules.sqlite3")
        modal = ui.AutoBookModal(OWNER, store)
        components = modal.to_components()
        assert [c["type"] for c in components] == [18, 18, 18, 18]
        assert [c["component"]["type"] for c in components] == [3, 3, 3, 4]
        assert [o["value"] for o in components[0]["component"]["options"]] == list(ui.WEEKDAYS)
        for component in components[1:3]:
            assert len(component["component"]["options"]) == 24
            assert component["component"]["min_values"] == component["component"]["max_values"] == 1
        it = interaction()
        # Exercise the installed SDK's nested Label / Select submit handling.
        modal._refresh(it, [
            {"type": 18, "component": {"type": 3, "custom_id": modal.weekday.custom_id, "values": ["vrijdag"]}},
            {"type": 18, "component": {"type": 3, "custom_id": modal.start_time.custom_id, "values": ["18:00"]}},
            {"type": 18, "component": {"type": 3, "custom_id": modal.end_time.custom_id, "values": ["21:00"]}},
            {"type": 18, "component": {"type": 4, "custom_id": modal.notes.custom_id, "value": "  Padel met vrienden  "}},
        ], {})
        await modal.on_submit(it)
        await modal.on_submit(it)
        rules = store.list_rules()
        assert len(rules) == 1 and rules[0].day_of_week == 4 and rules[0].time_local == "18:00"
        assert rules[0].end_time_local == "21:00"
        assert rules[0].notes == "Padel met vrienden"
        assert "vrijdag 18:00–21:00" in it.edits[-1]["embed"].description
        view = ui.AutoBookView(OWNER, store, selected=rules[0].id)
        await view.toggle(it)
        assert store.get_rule(rules[0].id).enabled is False
        await view.toggle(it)
        assert store.get_rule(rules[0].id).enabled is True
        await view.remove(it)
        assert store.list_rules() == []
    asyncio.run(run())


@pytest.mark.parametrize("day,start,end,user", [
    ([], ["18:00"], ["21:00"], OWNER),
    (["vrijdag", "zaterdag"], ["18:00"], ["21:00"], OWNER),
    (["invalid"], ["18:00"], ["21:00"], OWNER),
    (["vrijdag"], [], ["21:00"], OWNER),
    (["vrijdag"], ["18:00"], [], OWNER),
    (["vrijdag"], ["21:00"], ["18:00"], OWNER),
    (["vrijdag"], ["18:00"], ["21:00"], 43),
])
def test_invalid_interval_or_non_owner_does_not_create_a_rule(tmp_path, day, start, end, user):
    async def run():
        store = RulesStore(tmp_path / "rules.sqlite3")
        modal = ui.AutoBookModal(OWNER, store)
        modal.weekday._values, modal.start_time._values, modal.end_time._values = day, start, end
        it = interaction(user)
        await modal.on_submit(it)
        assert store.list_rules() == []
        assert len(it.replies) == 1
    asyncio.run(run())


@pytest.mark.parametrize("end", [None, "21:00"])
def test_duplicate_interval_including_legacy_fixed_time_is_rejected(tmp_path, end):
    async def run():
        store = RulesStore(tmp_path / "rules.sqlite3")
        old = store.add_rule(4, "18:00", end_time_local=end)
        store.set_enabled(old.id, False)
        modal = ui.AutoBookModal(OWNER, store)
        modal.weekday._values = ["vrijdag"]
        modal.start_time._values, modal.end_time._values = ["18:00"], [end or "18:00"]
        it = interaction()
        await modal.on_submit(it)
        assert len(store.list_rules()) == 1
        assert "Hervatten" in it.replies[0]
    asyncio.run(run())


def test_bookings_menu_paginates_and_requires_selection_before_cancel_button():
    mine = [SimpleNamespace(id=i, start_dt=ui.today(), raw={"productId": 4385}) for i in range(30)]
    async def run():
        view = ui.BookingsView(OWNER, mine, {4385: "Padel 4"})
        assert not any(getattr(c, "label", "").startswith("Annuleer") for c in view.children)
        it = interaction()
        await view.next_page(it)
        second = it.edits[-1]["view"]
        assert len(second.children[0].options) == 5
        second.children[0]._values = ["29"]
        await second.children[0].callback(it)
        chosen = it.edits[-1]["view"]
        assert chosen.selected == 29
        assert any(getattr(c, "label", "") == "Annuleer deze reservatie" for c in chosen.children)
    asyncio.run(run())


@pytest.mark.parametrize("cancel_fails,reload_fails", [(False, False), (True, False), (False, True)])
def test_cancel_panel_acknowledges_first_writes_once_and_reports_verified_result(monkeypatch, cancel_fails, reload_fails):
    calls, shared = [], []
    mine = [SimpleNamespace(id=123, start_dt=ui.today(), raw={"productId": 4385})]
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def me(self): return {"id": 99}
    monkeypatch.setattr(ui, "BackboneClient", Client)
    async def run():
        it = interaction()
        def cancel(client, **kwargs):
            assert it.defers == [{}]
            assert it.client._booking_lock.locked()
            calls.append(kwargs)
            if cancel_fails: raise RuntimeError("staat nog actief")
            return {"statusCode": 2}
        async def sync_shared(interaction, owner, booking_id):
            shared.append((owner, booking_id))
        async def reload(owner):
            if reload_fails: raise TimeoutError("reload failed")
            return ui.BookingsView(owner, [], {})
        monkeypatch.setattr(ui, "cancel_booking", cancel)
        monkeypatch.setattr(ui, "cancel_shared_booking", sync_shared)
        monkeypatch.setattr(ui.BookingsView, "load", reload)
        view = ui.BookingsView(OWNER, mine, {4385: "Padel 4"}, selected=123)
        assert "rode knop" in view.embed().description
        await asyncio.gather(view.cancel(it), view.cancel(it))
        assert calls == [{"booking_id": 123, "member_id": 99}]
        assert len(it.defers) == 1
        if cancel_fails:
            assert shared == []
            assert "Annulering niet bevestigd" in it.edits[-1]["content"]
            assert it.edits[-1]["view"] is None
        else:
            assert shared == [(OWNER, 123)]
            assert "Reservatie geannuleerd" in it.edits[-1]["content"]
            assert (it.edits[-1]["view"] is None) is reload_fails
    asyncio.run(run())


def test_multiple_bookings_can_be_selected_and_shared_without_bulk_cancel(monkeypatch):
    mine = [SimpleNamespace(id=i, start_dt=ui.today(), raw={"productId": 4385}) for i in (100, 101)]
    shared = []
    async def publish(it, owner, ids, **kwargs): shared.append((owner, ids, kwargs))
    monkeypatch.setattr(ui, "publish_bookings", publish)
    monkeypatch.setattr(ui, "BackboneClient", lambda: pytest.fail("A multi-selection must not cancel reservations"))
    async def run():
        view = ui.BookingsView(OWNER, mine, {4385: "Padel 4"})
        select = view.children[0]
        assert select.min_values == 0 and select.max_values == 2
        select._values = ["100", "101"]
        it = interaction()
        await select.callback(it)
        chosen = it.edits[-1]["view"]
        assert chosen.selected_ids == (100, 101) and chosen.selected is None
        assert all(option.default for option in chosen.children[0].options)
        labels = [getattr(c, "label", "") for c in chosen.children]
        assert "Deel selectie (2)" in labels and "Deel selectie + @everyone" in labels
        assert not any(label.startswith("Annuleer") for label in labels)
        assert "Geselecteerd (2)" in chosen.embed().description
        await chosen.share(it)
        await chosen.share_everyone(it)
        assert shared == [(OWNER, (100, 101), {}), (OWNER, (100, 101), {"tag_everyone": True})]
        await chosen.cancel(it)
        assert "Selecteer één reservatie" in it.replies[-1]
    asyncio.run(run())


def test_multi_selection_survives_pagination_and_respects_five_booking_limit():
    mine = [SimpleNamespace(id=i, start_dt=ui.today(), raw={"productId": 4385}) for i in range(30)]
    async def run():
        it = interaction()
        view = ui.BookingsView(OWNER, mine, {}, selected=(0, 1, 2))
        await view.next_page(it)
        second = it.edits[-1]["view"]
        assert second.selected_ids == (0, 1, 2)
        assert second.children[0].max_values == 2
        second.children[0]._values = ["28", "29"]
        await second.children[0].callback(it)
        chosen = it.edits[-1]["view"]
        assert chosen.selected_ids == (0, 1, 2, 28, 29)
        await chosen.previous_page(it)
        first = it.edits[-1]["view"]
        assert first.selected_ids == chosen.selected_ids
        assert first.children[0].max_values == 3
        assert {int(o.value) for o in first.children[0].options if o.default} == {0, 1, 2}
        first.children[0]._values = []
        await first.children[0].callback(it)
        reduced = it.edits[-1]["view"]
        assert reduced.selected_ids == (28, 29)
        await reduced.clear_selection(it)
        assert it.edits[-1]["view"].selected_ids == ()
        with pytest.raises(ValueError, match="maximaal 5"):
            ui.BookingsView(OWNER, mine, {}, selected=range(6))
    asyncio.run(run())
