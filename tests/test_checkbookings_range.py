import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord
import pytest

from padelbot import discord_ui as ui
from padelbot.booking import Slot
from padelbot.config import settings
from padelbot.discord_bot import make_bot


OWNER = 42
BASE = datetime(2026, 10, 24, tzinfo=ZoneInfo("Europe/Brussels"))


@pytest.fixture(autouse=True)
def local_today(monkeypatch):
    monkeypatch.setattr(ui, "today", lambda: BASE)


def interaction(user=OWNER):
    events = []
    async def edit(**kwargs):
        events.append(("edit", kwargs))
    async def send(content=None, **kwargs):
        if "file" in kwargs:
            kwargs["attachment_text"] = kwargs["file"].fp.read().decode("utf-8")
        events.append(("send", {"content": content, **kwargs}))
        return SimpleNamespace(jump_url="https://discord.com/channels/1/2/3", flags=SimpleNamespace(ephemeral=False))
    async def defer(**kwargs):
        events.append(("defer", kwargs))
    return SimpleNamespace(user=SimpleNamespace(id=user), events=events,
        response=SimpleNamespace(edit_message=edit, send_message=send, defer=defer),
        followup=SimpleNamespace(send=send), edit_original_response=edit)


def slot_on(day, hour=18, court=4385, minute=0):
    start = (BASE + timedelta(days=day)).replace(hour=hour, minute=minute)
    return Slot(court, 4610, start.isoformat(), (start + timedelta(minutes=89)).isoformat(), None, {})


@pytest.mark.parametrize("value,expected", [
    ("0", (0, 0)), ("1", (1, 1)), ("2", (2, 2)), ("365", (365, 365)),
    ("0-3", (0, 3)), (" 0 - 3 ", (0, 3)), ("1–6", (1, 6)),
    ("0-5", (0, 5)), ("0 -5", (0, 5)), (" 0 - 5 ", (0, 5)),
    ("3-3", (3, 3)), ("0-365", (0, 365)),
])
def test_single_days_and_inclusive_ranges(value, expected):
    assert ui.parse_day_range(value) == expected


@pytest.mark.parametrize("value", ["", " ", "-1", "3-0", "366", "0-366", "0-3-5", "0,3", "1.5", "vandaag", "0--3"])
def test_invalid_day_ranges_have_a_clear_error(value):
    with pytest.raises(ValueError, match="0-5"):
        ui.parse_day_range(value)


@pytest.mark.parametrize("day,expected", [
    ("0", (BASE,)),
    ("1", (BASE + timedelta(days=1),)),
    ("2", (BASE + timedelta(days=2),)),
    ("0-3", (BASE, BASE + timedelta(days=3))),
    ("0-5", (BASE, BASE + timedelta(days=5))),
    ("0 -5", (BASE, BASE + timedelta(days=5))),
    ("0 - 5", (BASE, BASE + timedelta(days=5))),
    ("3-3", (BASE + timedelta(days=3),)),
])
def test_command_loads_exact_requested_dates(tmp_path, monkeypatch, day, expected):
    monkeypatch.setattr(settings, "padelbot_rules_file", tmp_path / "rules.sqlite3")
    monkeypatch.setattr(settings, "discord_owner_id", OWNER)
    requested = []
    async def load(owner, *dates):
        assert owner == OWNER
        requested.append(dates)
        return SimpleNamespace(embed=lambda: discord.Embed(title="Beschikbaarheid"))
    monkeypatch.setattr(ui.DaySlotsView, "load", load)
    monkeypatch.setattr(ui.SlotsRangeView, "load", load)
    async def run():
        bot = make_bot()
        it = interaction()
        try:
            await bot.tree.get_command("checkbookings").callback(it, day)
            assert requested == [expected]
            assert it.events[0] == ("defer", {"thinking": True, "ephemeral": True})
            assert it.events[-1][1]["ephemeral"] is True
        finally:
            await bot.close()
    asyncio.run(run())


def test_invalid_range_is_rejected_before_fetching(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "padelbot_rules_file", tmp_path / "rules.sqlite3")
    monkeypatch.setattr(settings, "discord_owner_id", OWNER)
    monkeypatch.setattr(ui, "fetch_range", lambda *a: pytest.fail("Invalid range must not fetch"))
    async def run():
        bot = make_bot()
        it = interaction()
        try:
            await bot.tree.get_command("checkbookings").callback(it, "3-0")
            assert len(it.events) == 1
            assert "0-5" in it.events[0][1]["content"]
            assert it.events[0][1]["ephemeral"] is True
        finally:
            await bot.close()
    asyncio.run(run())


@pytest.mark.parametrize("last_day", [3, 5])
def test_range_fetch_is_one_inclusive_window(monkeypatch, last_day):
    requests = []
    slots = [slot_on(0), slot_on(last_day)]
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
    def find(client, **kwargs):
        requests.append(kwargs)
        return slots
    monkeypatch.setattr(ui, "BackboneClient", Client)
    monkeypatch.setattr(ui, "find_slots_window", find)
    monkeypatch.setattr(ui, "get_product_names", lambda client, ids: {i: "Padel 4" for i in ids})
    async def run():
        view = await ui.SlotsRangeView.load(OWNER, BASE, BASE + timedelta(days=last_day))
        assert requests == [{"start_date": BASE, "days": last_day + 1}]
        description = view.embed().description
        for day in range(last_day + 1):
            assert f"{BASE + timedelta(days=day):%d/%m/%Y}" in description
        assert description.count("Geen beschikbare padelvelden.") == last_day - 1
        assert len(view.children[0].options) == last_day + 1
        assert view.children[0].options[-1].value == (BASE + timedelta(days=last_day)).date().isoformat()
        assert "Padel 4" in description
    asyncio.run(run())


def test_future_range_limits_queries_and_labels_unopened_days(monkeypatch):
    requests = []
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(settings, "padel_opens_days_ahead", 7)
    monkeypatch.setattr(ui, "BackboneClient", Client)
    monkeypatch.setattr(ui, "find_slots_window", lambda client, **kw: requests.append(kw) or [])
    monkeypatch.setattr(ui, "get_product_names", lambda *a: {})
    ui.fetch_range(BASE, BASE + timedelta(days=365))
    assert requests == [{"start_date": BASE, "days": 8}]
    assert ui.fetch_range(BASE + timedelta(days=10), BASE + timedelta(days=20)) == ([], {})
    assert len(requests) == 1
    overview = ui.AvailabilityOverview(BASE + timedelta(days=10), BASE + timedelta(days=20), [], {})
    assert "Nog niet boekbaar" in overview.embed().description
    assert "Geen beschikbare" not in overview.embed().description


def test_day_choice_and_back_keep_the_range_through_booking_confirmation(monkeypatch):
    slots = [slot_on(0), slot_on(1)]
    monkeypatch.setattr(ui, "book", lambda *a, **kw: pytest.fail("Browsing must not book"))
    def quote(selection):
        slot = next(s for s in selection.slots if s.bookable_product_id == selection.court)
        return ui.BookingQuote(slot, OWNER, Decimal("8.00"), time.monotonic() + 120)
    monkeypatch.setattr(ui.DaySlotsView, "prepare_confirmation", quote)
    monkeypatch.setattr(ui, "fetch_day", lambda target: ([slot_on((target.date() - BASE.date()).days)], {4385: "Padel 4"}))
    async def run():
        overview = ui.AvailabilityOverview(BASE, BASE + timedelta(days=3), slots, {4385: "Padel 4"})
        view = ui.SlotsRangeView(OWNER, overview)
        it = interaction()
        view.children[0]._values = [(BASE + timedelta(days=1)).date().isoformat()]
        await view.children[0].callback(it)
        day_view = it.events[-1][1]["view"]
        assert day_view.target == BASE + timedelta(days=1)
        assert day_view.slots == [slots[1]]
        # Refreshing a day must also preserve the range in subsequent callbacks.
        await day_view.refresh(it)
        day_view = it.events[-1][1]["view"]
        day_view.children[0]._values = ["18:00"]
        await day_view.children[0].callback(it)
        time_view = it.events[-1][1]["view"]
        time_view.children[1]._values = ["4385"]
        await time_view.children[1].callback(it)
        confirmation = it.events[-1][1]["view"]
        assert isinstance(confirmation, ui.ConfirmBookingView)
        assert confirmation.quote.slot.start_dt.date() == slots[1].start_dt.date()
        assert confirmation.selection.overview is overview
        await confirmation.back(it)
        await it.events[-1][1]["view"].back_to_overview(it)
        restored = it.events[-1][1]["view"]
        assert isinstance(restored, ui.SlotsRangeView)
        assert restored.overview is overview
    asyncio.run(run())


def large_overview():
    slots = [slot_on(day, hour, court, minute) for day in range(4)
             for hour in range(6, 23) for minute in (0, 30) for court in (4385, 4386)]
    return ui.AvailabilityOverview(BASE, BASE + timedelta(days=3), slots,
                                   {4385: "Padelveld vier " * 4, 4386: "Padelveld vijf " * 4})


def test_large_overviews_preserve_all_slots_with_valid_discord_pages():
    overview = large_overview()
    assert len(overview.pages) > 1
    assert sum(body.count("**18:00**") for body, _ in overview.pages) == 4
    assert sum(body.count("**22:30**") for body, _ in overview.pages) == 4
    async def run():
        it = interaction()
        for page in range(len(overview.pages)):
            view = ui.SlotsRangeView(OWNER, overview, page=page)
            assert len(view.embed().description) <= 4096
            assert len(view.embed()) <= 6000
            assert len(view.children[0].options) <= 25
            assert len(view.to_components()) <= 5
            if page + 1 < view.pages:
                await view.next_page(it)
                assert it.events[-1][1]["view"].page == page + 1
        empty = ui.AvailabilityOverview(BASE, BASE + timedelta(days=365), [], {})
        all_days = {day for _, days in empty.pages for day in days}
        assert len(all_days) == 366
        assert all(len(days) <= 25 for _, days in empty.pages)
    asyncio.run(run())


def test_sharing_posts_all_days_once_and_public_buttons_only_browse():
    async def run():
        overview = large_overview()
        view = ui.SlotsRangeView(OWNER, overview, page=1)
        it = interaction()
        await asyncio.gather(view.share(it), ui.SlotsRangeView(OWNER, overview).share(it))
        posts = [kw for event, kw in it.events if event == "send" and kw.get("ephemeral") is False]
        assert len(posts) == 1
        assert it.events[0] == ("defer", {"thinking": False})
        posted = posts[0]
        assert posted["allowed_mentions"].to_dict() == {"parse": []}
        assert posted["view"].public is True
        assert posted["view"].page == 0
        assert all(isinstance(child, discord.ui.Button) for child in posted["view"].children)
        assert {child.label for child in posted["view"].children} == {"Vorige pagina", "Volgende pagina"}
        for day in range(4):
            assert f"{BASE + timedelta(days=day):%d/%m/%Y}" in posted["attachment_text"]
        assert posted["attachment_text"].count("**22:30**") == 4
        guest = interaction(user=43)
        public = posted["view"]
        assert await public.interaction_check(guest) is True
        await public.next_page(guest)
        assert guest.events[-1][1]["view"].public is True
        assert await view.interaction_check(guest) is False
        await view.share(guest)
        assert guest.events[-1][1]["ephemeral"] is True
        assert overview.shared_url == "https://discord.com/channels/1/2/3"
    asyncio.run(run())


def test_single_day_sharing_survives_time_navigation_without_duplicate_posts():
    async def run():
        view = ui.DaySlotsView(OWNER, BASE, [slot_on(0)], {4385: "Padel 4"})
        it = interaction()
        await view.share(it)
        view.children[0]._values = ["18:00"]
        await view.children[0].callback(it)
        selected = it.events[-1][1]["view"]
        await selected.share(it)
        posts = [kw for event, kw in it.events if event == "send" and kw.get("ephemeral") is False]
        assert len(posts) == 1
        assert posts[0]["view"] is None
        assert "24/10/2026" in posts[0]["embed"].description
        assert "18:00" in posts[0]["embed"].description
    asyncio.run(run())


def test_failed_share_is_not_automatically_retried():
    async def run():
        overview = ui.AvailabilityOverview(BASE, BASE, [], {})
        it = interaction()
        attempts = []
        async def send(content=None, **kwargs):
            if kwargs.get("ephemeral") is False:
                attempts.append(True)
                raise RuntimeError("Connection lost after sending")
            it.events.append(("send", {"content": content, **kwargs}))
        it.followup.send = send
        await ui.share_availability(it, OWNER, overview)
        await ui.share_availability(it, OWNER, overview)
        assert attempts == [True]
        assert overview.shared_url is None
        assert any("Delen niet bevestigd" in kw.get("content", "") for event, kw in it.events)
    asyncio.run(run())


def test_private_reply_is_not_reported_as_shared():
    async def run():
        overview = ui.AvailabilityOverview(BASE, BASE, [], {})
        it = interaction()
        async def send(content=None, **kwargs):
            it.events.append(("send", {"content": content, **kwargs}))
            return SimpleNamespace(jump_url="https://discord.com/channels/1/2/3", flags=SimpleNamespace(ephemeral=True))
        it.followup.send = send
        await ui.share_availability(it, OWNER, overview)
        assert overview.shared_url is None
        assert "Delen niet bevestigd" in it.events[-1][1]["content"]
    asyncio.run(run())
