import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import discord

from padelbot import availability_watch as aw
from padelbot import watch_ui, discord_ui, booking
from padelbot.config import settings

TZ = ZoneInfo("Europe/Brussels")
NOW = datetime(2030, 6, 5, 12, tzinfo=TZ)


def slot(hour, minute=0, court=4385, *, day=NOW.date()):
    start = datetime.combine(day, datetime.min.time(), TZ).replace(hour=hour, minute=minute)
    return SimpleNamespace(start_dt=start, bookable_product_id=court)


def store_for(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "discord_owner_id", 42)
    monkeypatch.setattr(aw, "local_now", lambda: NOW)
    monkeypatch.setattr(watch_ui, "local_now", lambda: NOW)
    return aw.WatchStore(tmp_path / "rules.sqlite3")


def connect_chat(bot, watch, send, *, channel_id=8, guild_id=None, user=42, expired=False):
    interaction = SimpleNamespace(client=bot, channel_id=channel_id, guild_id=guild_id,
        user=SimpleNamespace(id=user), is_expired=lambda: expired,
        is_guild_integration=lambda: bool(guild_id),
        app_permissions=SimpleNamespace(mention_everyone=True), followup=SimpleNamespace(send=send))
    watch_ui.remember_watch_interaction(bot, bot.watch_store, watch, interaction)
    return interaction


def test_watches_survive_restart_are_deduplicated_and_expire(tmp_path, monkeypatch):
    store = store_for(tmp_path, monkeypatch)
    watch, created = store.create(42, NOW.date(), 13, 20)
    assert created
    store.save_check(watch.id, {"13:30": [4384, 4385]})
    restarted = aw.WatchStore(store.path)
    same, created = restarted.create(42, NOW.date(), 13, 20)
    assert not created and same.id == watch.id
    assert same.snapshot == {"13:30": [4384, 4385]}
    assert not restarted.stop(watch.id, 43)
    restarted.expire(now=NOW.replace(hour=20))
    assert restarted.list_active() == []
    assert not restarted.get(watch.id).active


@pytest.mark.parametrize("day,from_hour,to_hour,now", [
    (NOW.date(), 20, 13, NOW), (NOW.date(), -1, 20, NOW),
    (NOW.date(), 13, 25, NOW), (NOW.date(), 13, 20, NOW.replace(hour=21)),
    ((NOW - timedelta(days=1)).date(), 13, 20, NOW),
])
def test_invalid_windows_create_nothing(tmp_path, monkeypatch, day, from_hour, to_hour, now):
    store = store_for(tmp_path, monkeypatch)
    with pytest.raises(ValueError): store.create(42, day, from_hour, to_hour, now=now)
    assert not store.list_active()


def test_watch_follows_start_times_inclusive_and_only_reports_last_court_disappearing(tmp_path, monkeypatch):
    store = store_for(tmp_path, monkeypatch)
    watch, _ = store.create(42, NOW.date(), 13, 20)
    slots = [slot(12, 30), slot(13), slot(13, court=4384), slot(20), slot(20, 30), slot(14, day=(NOW + timedelta(days=1)).date())]
    snapshot = aw.snapshot_slots(watch, slots)
    assert snapshot == {"13:00": [4384, 4385], "20:00": [4385]}
    watch = replace(watch, snapshot=snapshot)
    assert aw.changes(watch, {"13:00": [4385], "20:00": [4385]}) == ([], [])
    assert aw.changes(watch, {"15:00": [4385], "20:00": [4385]}) == (["15:00"], ["13:00"])
    assert aw.changes(watch, {"20:00": [4385]}, now=NOW.replace(hour=14)) == ([], [])


def test_poll_batches_day_reads_and_preserves_state_without_booking_or_duplicate_alerts(tmp_path, monkeypatch):
    store = store_for(tmp_path, monkeypatch)
    first, _ = store.create(42, NOW.date(), 13, 20)
    second, _ = store.create(42, NOW.date(), 15, 19)
    store.save_check(first.id, {"13:00": [4385], "15:00": [4385]})
    store.save_check(second.id, {"15:00": [4385]})
    calls, messages = [], []
    def fetch(day):
        calls.append(day)
        return [slot(15), slot(17)], {4385: "Padel 4"}
    monkeypatch.setattr(aw, "fetch_watch_day", fetch)
    monkeypatch.setattr(booking, "book", lambda *a, **kw: pytest.fail("A notification watcher must not book"))
    async def send(**kwargs): messages.append(kwargs)
    bot = SimpleNamespace(watch_store=store, _availability_watch_lock=asyncio.Lock(), get_user=lambda uid: SimpleNamespace(send=send))
    connect_chat(bot, first, send)
    connect_chat(bot, second, send)
    async def run():
        await aw.poll_watches(bot)
        assert calls == [NOW.date()]
        assert len(messages) == 2
        fields = messages[0]["embed"].fields
        assert "Vrijgekomen" in fields[0].name and "17:00" in fields[0].value
        assert "Volzet" in fields[1].name and "13:00" in fields[1].value
        assert messages[0]["view"].is_persistent()
        assert all(message["ephemeral"] for message in messages)
        bot.watch_store = aw.WatchStore(store.path)
        await aw.poll_watches(bot)
        assert len(messages) == 2, "Restart must not repeat already delivered availability"
        def failure(day): raise TimeoutError("private details")
        monkeypatch.setattr(aw, "fetch_watch_day", failure)
        await aw.poll_watches(bot)
        assert len(messages) == 2, "Network failure must not be reported as fully booked"
        assert bot.watch_store.get(first.id).snapshot == {"15:00": [4385], "17:00": [4385]}
        assert "private details" not in bot.watch_store.get(first.id).error
        store.stop(first.id, 42)
        store.stop(second.id, 42)
        calls.clear()
        monkeypatch.setattr(aw, "fetch_watch_day", fetch)
        await aw.poll_watches(bot)
        assert calls == [] and len(messages) == 2
    asyncio.run(run())


def test_failed_private_chat_delivery_is_retried_without_consuming_the_change(tmp_path, monkeypatch):
    store = store_for(tmp_path, monkeypatch)
    watch, _ = store.create(42, NOW.date(), 13, 20)
    store.save_check(watch.id, {})
    monkeypatch.setattr(aw, "fetch_watch_day", lambda day: ([slot(15)], {4385: "Padel 4"}))
    messages = []
    fail = True
    async def send(**kwargs):
        if fail: raise RuntimeError("chat unavailable")
        messages.append(kwargs)
    bot = SimpleNamespace(watch_store=store, _availability_watch_lock=asyncio.Lock(), get_user=lambda uid: SimpleNamespace(send=send))
    connect_chat(bot, watch, send)
    async def run():
        nonlocal fail
        await aw.poll_watches(bot)
        assert store.get(watch.id).snapshot == {}
        assert store.get(watch.id).error
        fail = False
        await aw.poll_watches(bot)
        assert len(messages) == 1 and store.get(watch.id).error is None
        await aw.poll_watches(bot)
        assert len(messages) == 1
    asyncio.run(run())


@pytest.mark.parametrize("owner", [42, 43])
def test_watch_stop_button_is_owner_only_and_never_cancels_a_reservation(tmp_path, monkeypatch, owner):
    store = store_for(tmp_path, monkeypatch)
    watch, _ = store.create(42, NOW.date(), 13, 20)
    monkeypatch.setattr(discord_ui, "cancel_booking", lambda *a, **kw: pytest.fail("Stop watcher must not cancel a reservation"))
    events = []
    async def send(content, **kwargs): events.append(("send", content, kwargs))
    async def defer(**kwargs): events.append(("defer", kwargs))
    async def edit(**kwargs): events.append(("edit", kwargs))
    async def run():
        it = SimpleNamespace(user=SimpleNamespace(id=owner), response=SimpleNamespace(defer=defer, send_message=send),
            edit_original_response=edit, client=SimpleNamespace(_availability_watch_lock=asyncio.Lock()))
        view = watch_ui.WatchControlsView(store, watch.id)
        assert view.is_persistent()
        await view.act(it, "stop")
        if owner == 43:
            assert store.get(watch.id).active
            assert events[0][0] == "send" and events[0][2]["ephemeral"]
        else:
            assert not store.get(watch.id).active
            assert events[0] == ("defer", {"thinking": True, "ephemeral": True})
            assert "Watcher gestopt" in events[-1][1]["content"]
            assert events[-1][1]["view"].children[-1].disabled
    asyncio.run(run())


def test_opening_watched_slots_only_opens_booking_ui(tmp_path, monkeypatch):
    store = store_for(tmp_path, monkeypatch)
    watch, _ = store.create(42, NOW.date(), 13, 20)
    # The real day view needs Slot's timestamps, not just the start property.
    def real_slot(hour):
        start = NOW.replace(hour=hour)
        return booking.Slot(4385, 4610, start.isoformat(), (start + timedelta(minutes=89)).isoformat(), None, {})
    monkeypatch.setattr(watch_ui, "fetch_watch_day", lambda day: ([real_slot(12), real_slot(15), real_slot(21)], {4385: "Padel 4"}))
    monkeypatch.setattr(discord_ui.DaySlotsView, "reserve_now", lambda *a, **kw: pytest.fail("Viewing a watch does not authorize payment"))
    edits = []
    async def defer(**kwargs): pass
    async def edit(**kwargs): edits.append(kwargs)
    async def run():
        it = SimpleNamespace(user=SimpleNamespace(id=42), response=SimpleNamespace(defer=defer), edit_original_response=edit,
            client=SimpleNamespace(), channel_id=8, guild_id=None, is_guild_integration=lambda: False)
        await watch_ui.WatchControlsView(store, watch.id).act(it, "slots")
        view = edits[-1]["view"]
        assert isinstance(view, discord_ui.DaySlotsView)
        assert [slot.start_dt.astimezone(TZ).hour for slot in view.slots] == [15]
        assert store.get(watch.id).active
    asyncio.run(run())


def public_interaction(store, *, guild_id=7, user=42, permission=True, send_error=None):
    events, posts, edits, registered = [], [], [], []
    async def defer(**kwargs): events.append(("defer", kwargs))
    async def response_send(content, **kwargs): events.append(("response", content, kwargs))
    async def edit(**kwargs): edits.append(kwargs)
    async def edit_shared(message_id, **kwargs): events.append(("shared-edit", message_id, kwargs))
    async def send(content=None, **kwargs):
        if kwargs.get("ephemeral") is False:
            posts.append({"content": content, **kwargs})
            if send_error: raise send_error
        else:
            events.append(("followup", content, kwargs))
        return SimpleNamespace(id=100, flags=SimpleNamespace(ephemeral=kwargs.get("ephemeral", False)), edit=edit)
    bot = SimpleNamespace(watch_store=store, _availability_watch_lock=asyncio.Lock(), user=SimpleNamespace(id=900),
        get_guild=lambda gid: SimpleNamespace(id=gid, me=object()), add_view=lambda *a, **kw: registered.append((a, kw)))
    it = SimpleNamespace(client=bot, user=SimpleNamespace(id=user), channel_id=8, guild_id=guild_id,
        message=SimpleNamespace(id=100, author=SimpleNamespace(id=900)),
        context=SimpleNamespace(dm_channel=False), app_permissions=SimpleNamespace(mention_everyone=permission),
        response=SimpleNamespace(defer=defer, send_message=response_send), followup=SimpleNamespace(send=send, edit_message=edit_shared),
        edit_original_response=edit, events=events, posts=posts, edits=edits, registered=registered,
        is_expired=lambda: False, is_guild_integration=lambda: bool(guild_id))
    return it


@pytest.mark.parametrize("guild_id", [7, None])
def test_public_choice_pings_once_binds_destination_and_survives_restart(tmp_path, monkeypatch, guild_id):
    store = store_for(tmp_path, monkeypatch)
    watch, _ = store.create(42, NOW.date(), 13, 20)
    monkeypatch.setattr(watch_ui, "fetch_watch_day", lambda day: ([slot(15)], {4385: "Padel 4"}))
    async def run():
        it = public_interaction(store, guild_id=guild_id)
        view = watch_ui.WatchControlsView(store, watch.id)
        assert {c.label for c in view.children} >= {"Privé", "Openbaar + @everyone"}
        await asyncio.gather(view.act(it, "public"), view.act(it, "public"))
        assert len(it.posts) == 1
        assert "@everyone" in it.posts[0]["content"]
        assert it.posts[0]["allowed_mentions"].everyone is True
        assert it.posts[0]["allowed_mentions"].users is False
        assert it.posts[0]["allowed_mentions"].roles is False
        assert it.posts[0]["view"].is_persistent()
        assert it.events[0] == ("defer", {"thinking": False})
        persisted = aw.WatchStore(store.path).get(watch.id)
        assert persisted.visibility == "public" and not persisted.public_pending
        assert persisted.public_channel_id == 8 and persisted.public_message_id == 100
        assert persisted.public_live is bool(guild_id)
        assert "deze chat" in it.edits[-1]["content"] and "DM" not in it.edits[-1]["content"]
        assert persisted.origin_channel_id == 8 and persisted.origin_guild_id == guild_id
    asyncio.run(run())


@pytest.mark.parametrize("case", ["other-user", "no-everyone-permission", "bot-dm"])
def test_public_choice_checks_owner_destination_and_ping_permission_before_sending(tmp_path, monkeypatch, case):
    store = store_for(tmp_path, monkeypatch)
    watch, _ = store.create(42, NOW.date(), 13, 20)
    monkeypatch.setattr(watch_ui, "fetch_watch_day", lambda day: pytest.fail("Rejected sharing must not read KUL"))
    async def run():
        it = public_interaction(store, user=43 if case == "other-user" else 42, permission=case != "no-everyone-permission")
        if case == "bot-dm": it.context.dm_channel = True
        await watch_ui.WatchControlsView(store, watch.id).act(it, "public")
        assert it.events[0][0] == "response" and it.events[0][2]["ephemeral"]
        assert it.posts == []
        assert store.get(watch.id).visibility == "private" and not store.get(watch.id).public_pending
    asyncio.run(run())


def test_uncertain_public_post_is_not_automatically_sent_or_pinged_again(tmp_path, monkeypatch):
    store = store_for(tmp_path, monkeypatch)
    watch, _ = store.create(42, NOW.date(), 13, 20)
    monkeypatch.setattr(watch_ui, "fetch_watch_day", lambda day: ([], {}))
    async def run():
        it = public_interaction(store, send_error=TimeoutError("possibly posted"))
        await watch_ui.WatchControlsView(store, watch.id).act(it, "public")
        assert len(it.posts) == 1
        assert store.get(watch.id).public_pending
        await watch_ui.WatchControlsView(aw.WatchStore(store.path), watch.id).act(it, "public")
        assert len(it.posts) == 1
        assert "eerdere deelactie" in it.events[-1][1]
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["private", "group-public", "server-public", "server-missing-permission", "anchor-error"])
def test_notifications_route_by_visibility_and_all_public_posts_ping_everyone(tmp_path, monkeypatch, mode):
    store = store_for(tmp_path, monkeypatch)
    watch, _ = store.create(42, NOW.date(), 13, 20)
    store.save_check(watch.id, {})
    if mode != "private":
        assert store.begin_publish(watch.id, 42)
        store.finish_publish(watch.id, 42, channel_id=8, message_id=100, guild_id=7 if mode != "group-public" else None, live=mode != "group-public")
    public, private, anchor_updates = [], [], []
    async def server_send(**kwargs):
        public.append(kwargs)
        return SimpleNamespace(flags=SimpleNamespace(ephemeral=False))
    async def owner_send(**kwargs): private.append(kwargs)
    async def anchor_edit(**kwargs):
        anchor_updates.append(kwargs)
        if mode == "anchor-error": raise RuntimeError("anchor gone")
    guild = SimpleNamespace(id=7, me=object())
    channel = SimpleNamespace(guild=guild, permissions_for=lambda member: SimpleNamespace(mention_everyone=mode != "server-missing-permission"),
        send=server_send, get_partial_message=lambda mid: SimpleNamespace(edit=anchor_edit))
    bot = SimpleNamespace(watch_store=store, _availability_watch_lock=asyncio.Lock(),
        get_user=lambda uid: pytest.fail("Watchers must never send DMs"), get_channel=lambda cid: channel)
    watch = store.get(watch.id)
    interaction = connect_chat(bot, watch, server_send if mode == "group-public" else owner_send,
        guild_id=None if mode == "group-public" else 7)
    async def edit_shared(message_id, **kwargs): await anchor_edit(**kwargs)
    interaction.followup.edit_message = edit_shared
    monkeypatch.setattr(aw, "fetch_watch_day", lambda day: ([slot(15)], {4385: "Padel 4"}))
    async def run():
        await aw.poll_watches(bot)
        if mode in ("server-public", "group-public", "anchor-error"):
            assert len(public) == 1 and private == []
            assert "@everyone" in public[0]["content"]
            assert public[0]["allowed_mentions"].everyone is True
            assert len(anchor_updates) == 1 and not anchor_updates[0]["allowed_mentions"].everyone
        elif mode == "private":
            assert public == [] and len(private) == 1
            assert private[0]["ephemeral"]
            assert not private[0]["allowed_mentions"].everyone
        else:
            assert public == private == []
            assert store.get(watch.id).snapshot == {}
            assert "kanaalrechten" in store.get(watch.id).error
        await aw.poll_watches(bot)
        assert len(public) + len(private) == (0 if mode == "server-missing-permission" else 1), "No duplicate ping when the shared overview update fails"
    asyncio.run(run())


def test_identical_watches_are_bound_and_deduplicated_per_origin_chat(tmp_path, monkeypatch):
    store = store_for(tmp_path, monkeypatch)
    first, _ = store.create(42, NOW.date(), 13, 20, channel_id=8, guild_id=7)
    second, created = store.create(42, NOW.date(), 13, 20, channel_id=9)
    assert created and second.id != first.id
    restarted = aw.WatchStore(store.path)
    same, created = restarted.create(42, NOW.date(), 13, 20, channel_id=8, guild_id=7)
    assert not created and same.id == first.id
    assert same.origin_channel_id == 8 and same.origin_guild_id == 7
    assert not restarted.bind_origin(first.id, 42, 9, None)
    async def run():
        it = public_interaction(restarted)
        it.channel_id = 9
        monkeypatch.setattr(watch_ui, "fetch_watch_day", lambda day: pytest.fail("Wrong chat must not read KUL"))
        await watch_ui.WatchControlsView(restarted, first.id).act(it, "public")
        assert it.posts == [] and it.events[0][2]["ephemeral"]
        assert same.chat_url in it.events[0][1]
        assert restarted.get(first.id).visibility == "private"
    asyncio.run(run())


@pytest.mark.parametrize("visibility,reason", [("private", "expired"), ("private", "restart"), ("public", "expired"), ("public", "restart")])
def test_expired_chat_access_keeps_changes_pending_until_refreshed_without_dm(tmp_path, monkeypatch, visibility, reason):
    store = store_for(tmp_path, monkeypatch)
    watch, _ = store.create(42, NOW.date(), 13, 20, channel_id=8)
    store.save_check(watch.id, {})
    if visibility == "public":
        store.begin_publish(watch.id, 42)
        store.finish_publish(watch.id, 42, channel_id=8, message_id=100, guild_id=None, live=False)
    it = public_interaction(store, guild_id=None)
    it.client.get_user = lambda uid: pytest.fail("Unavailable chat access must never fall back to DM")
    it.is_expired = lambda: reason == "expired"
    if reason == "expired":
        watch_ui.remember_watch_interaction(it.client, store, store.get(watch.id), it)
    monkeypatch.setattr(aw, "fetch_watch_day", lambda day: ([slot(15)], {}))
    monkeypatch.setattr(watch_ui, "fetch_watch_day", lambda day: ([slot(15)], {}))
    async def run():
        await aw.poll_watches(it.client)
        assert it.posts == [] and not any(event[0] == "followup" for event in it.events)
        assert store.get(watch.id).snapshot == {} and "Chattoegang" in store.get(watch.id).error
        it.is_expired = lambda: False
        if visibility == "public":
            await watch_ui.PublicWatchView(store, watch.id).refresh(it)
        else:
            await watch_ui.WatchControlsView(store, watch.id).act(it, "refresh")
        await aw.poll_watches(it.client)
        assert store.get(watch.id).snapshot == {"15:00": [4385]} and store.get(watch.id).error is None
        if visibility == "public":
            assert len(it.posts) == 1 and it.posts[0]["ephemeral"] is False
        else:
            notices = [event for event in it.events if event[0] == "followup"]
            assert len(notices) == 1 and notices[0][2]["ephemeral"]
        await aw.poll_watches(it.client)
        assert len(it.posts) == (1 if visibility == "public" else 0)
    asyncio.run(run())


def test_group_member_refresh_cannot_redirect_private_notifications_to_them(tmp_path, monkeypatch):
    store = store_for(tmp_path, monkeypatch)
    watch, _ = store.create(42, NOW.date(), 13, 20, channel_id=8)
    store.save_check(watch.id, {})
    store.begin_publish(watch.id, 42)
    store.finish_publish(watch.id, 42, channel_id=8, message_id=100, guild_id=None, live=False)
    owner = public_interaction(store, guild_id=None)
    member = public_interaction(store, guild_id=None, user=43)
    member.client = owner.client
    watch_ui.remember_watch_interaction(owner.client, store, store.get(watch.id), owner)
    monkeypatch.setattr(aw, "fetch_watch_day", lambda day: ([slot(15)], {}))
    monkeypatch.setattr(watch_ui, "fetch_watch_day", lambda day: ([slot(15)], {}))
    async def run():
        await watch_ui.PublicWatchView(store, watch.id).refresh(member)
        store.make_private(watch.id, 42)
        owner.client.get_user = lambda uid: pytest.fail("Private notifications must stay in their chat")
        await aw.poll_watches(owner.client)
        assert not any(event[0] == "followup" for event in member.events)
        notices = [event for event in owner.events if event[0] == "followup"]
        assert len(notices) == 1 and notices[0][2]["ephemeral"]
        assert owner.posts == member.posts == []
    asyncio.run(run())


def test_user_install_notification_quota_is_shared_and_renewed_by_fresh_action(tmp_path, monkeypatch):
    store = store_for(tmp_path, monkeypatch)
    first, _ = store.create(42, NOW.date(), 13, 20, channel_id=8)
    second, _ = store.create(42, NOW.date(), 14, 20, channel_id=8)
    store.save_check(first.id, {})
    store.save_check(second.id, {})
    it = public_interaction(store, guild_id=None)
    watch_ui.remember_watch_interaction(it.client, store, first, it, used_followups=5)
    watch_ui.remember_watch_interaction(it.client, store, second, it)
    it.client.get_user = lambda uid: pytest.fail("Exhausted quota must never send DMs")
    monkeypatch.setattr(aw, "fetch_watch_day", lambda day: ([slot(15)], {}))
    monkeypatch.setattr(watch_ui, "fetch_watch_day", lambda day: ([slot(15)], {}))
    async def run():
        await aw.poll_watches(it.client)
        assert store.get(first.id).snapshot == store.get(second.id).snapshot == {}
        assert "berichtlimiet" in store.get(first.id).error and "berichtlimiet" in store.get(second.id).error
        fresh = public_interaction(store, guild_id=None)
        fresh.client = it.client
        await watch_ui.WatchControlsView(store, first.id).act(fresh, "refresh")
        await aw.poll_watches(it.client)
        assert store.get(first.id).snapshot == {"15:00": [4385]}
        assert store.get(second.id).snapshot == {}
        notices = [event for event in fresh.events if event[0] == "followup"]
        assert len(notices) == 1 and notices[0][2]["ephemeral"]
    asyncio.run(run())


def test_switching_back_to_private_redacts_overview_and_future_notifications_stay_in_chat(tmp_path, monkeypatch):
    store = store_for(tmp_path, monkeypatch)
    watch, _ = store.create(42, NOW.date(), 13, 20)
    store.begin_publish(watch.id, 42)
    store.finish_publish(watch.id, 42, channel_id=8, message_id=100, guild_id=7, live=True)
    monkeypatch.setattr(discord_ui, "cancel_booking", lambda *a, **kw: pytest.fail("Privacy must not cancel bookings"))
    async def run():
        it = public_interaction(store)
        await watch_ui.WatchControlsView(store, watch.id).act(it, "private")
        persisted = aw.WatchStore(store.path).get(watch.id)
        assert persisted.visibility == "private" and not persisted.public_live
        redaction = next(event for event in it.events if event[0] == "shared-edit")
        assert redaction[1] == 100 and redaction[2]["embed"] is None and redaction[2]["view"] is None
        assert not redaction[2]["allowed_mentions"].everyone
        assert "alleen voor jou zichtbaar in deze chat" in it.edits[-1]["content"]
        store.save_check(watch.id, {})
        monkeypatch.setattr(aw, "fetch_watch_day", lambda day: ([slot(15)], {}))
        it.client.get_user = lambda uid: pytest.fail("Private watches must not send DMs")
        await aw.poll_watches(it.client)
        notice = next(event for event in it.events if event[0] == "followup")
        assert notice[2]["ephemeral"] and not notice[2]["allowed_mentions"].everyone
    asyncio.run(run())


@pytest.mark.parametrize("private_afterwards", [False, True])
def test_group_members_can_refresh_public_overview_but_private_data_is_never_exported(tmp_path, monkeypatch, private_afterwards):
    store = store_for(tmp_path, monkeypatch)
    watch, _ = store.create(42, NOW.date(), 13, 20)
    store.begin_publish(watch.id, 42)
    store.finish_publish(watch.id, 42, channel_id=8, message_id=100, guild_id=None, live=False)
    if private_afterwards:
        store.make_private(watch.id, 42)
        monkeypatch.setattr(watch_ui, "fetch_watch_day", lambda day: pytest.fail("Private watches must not be read by group members"))
    else:
        monkeypatch.setattr(watch_ui, "fetch_watch_day", lambda day: ([slot(15)], {4385: "Padel 4"}))
    async def run():
        it = public_interaction(store, guild_id=None, user=43)
        view = watch_ui.PublicWatchView(store, watch.id)
        await view.refresh(it)
        if private_afterwards:
            assert it.edits[-1]["embed"] is None and "nu privé" in it.edits[-1]["content"]
        else:
            assert "15:00" in it.edits[-1]["embed"].description
            assert not it.edits[-1]["allowed_mentions"].everyone
            assert "content" not in it.edits[-1], "Refreshing must retain the original @everyone text without re-pinging"
        # The same group member still cannot use private management or booking.
        await view.children[1].callback(it)
        assert it.events[-1][0] == "response"
        assert "Alleen de eigenaar" in it.events[-1][1]
    asyncio.run(run())


def test_schema_upgrade_preserves_existing_private_watches(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE availability_watches(id INTEGER PRIMARY KEY AUTOINCREMENT,owner_id INTEGER,day TEXT,from_hour INTEGER,to_hour INTEGER,active INTEGER,snapshot TEXT,checked_at TEXT,error TEXT)")
        conn.execute("INSERT INTO availability_watches VALUES (1,42,?,13,20,1,?,NULL,NULL)", (NOW.date().isoformat(), '{"15:00":[4385]}'))
    watch = aw.WatchStore(path).get(1)
    assert watch.id == 1 and watch.visibility == "private" and not watch.public_pending and not watch.public_live
    assert watch.snapshot == {"15:00": [4385]}
    assert aw.WatchStore(path).get(1) == watch


def test_uncertain_background_everyone_ping_is_not_repeated_or_sent_by_dm(tmp_path, monkeypatch):
    store = store_for(tmp_path, monkeypatch)
    watch, _ = store.create(42, NOW.date(), 13, 20)
    store.save_check(watch.id, {})
    store.begin_publish(watch.id, 42)
    store.finish_publish(watch.id, 42, channel_id=8, message_id=100, guild_id=7, live=True)
    attempts = []
    async def send(**kwargs):
        attempts.append(kwargs)
        raise TimeoutError("possibly pinged")
    guild = SimpleNamespace(id=7, me=object())
    channel = SimpleNamespace(guild=guild, permissions_for=lambda member: SimpleNamespace(mention_everyone=True), send=send)
    bot = SimpleNamespace(watch_store=store, _availability_watch_lock=asyncio.Lock(),
        get_user=lambda uid: pytest.fail("Public delivery must not fall back to DM"), get_channel=lambda cid: channel)
    monkeypatch.setattr(aw, "fetch_watch_day", lambda day: ([slot(15)], {4385: "Padel 4"}))
    async def run():
        await aw.poll_watches(bot)
        assert len(attempts) == 1 and store.get(watch.id).snapshot == {"15:00": [4385]}
        assert "niet bevestigd" in store.get(watch.id).error
        bot.watch_store = aw.WatchStore(store.path)
        await aw.poll_watches(bot)
        assert len(attempts) == 1
        assert bot.watch_store.get(watch.id).snapshot == {"15:00": [4385]}
    asyncio.run(run())
