import asyncio
from types import SimpleNamespace

import pytest
import discord
from discord import app_commands

from padelbot.config import settings
from padelbot.discord_bot import make_bot
from padelbot import discord_ui as ui


@pytest.mark.parametrize("guild_id", [0, 123456789012345678])
def test_group_commands_registered_globally_even_with_test_server(tmp_path, monkeypatch, guild_id):
    monkeypatch.setattr(settings, "padelbot_rules_file", tmp_path / "rules.sqlite3")
    monkeypatch.setattr(settings, "discord_guild_id", guild_id)
    monkeypatch.setattr(settings, "discord_owner_id", 123456789012345679)
    async def run():
        bot = make_bot()
        requests = []
        if guild_id:
            async def legacy_command(interaction: discord.Interaction): pass
            bot.tree.add_command(app_commands.Command(name="slots", description="Old slots command", callback=legacy_command), guild=discord.Object(id=guild_id))
        async def sync(*, guild=None):
            commands = bot.tree.get_commands(guild=guild)
            requests.append((getattr(guild, "id", None), [c.to_dict(bot.tree) for c in commands]))
            return commands
        monkeypatch.setattr(bot.tree, "sync", sync)
        try:
            await bot.setup_hook()
            assert requests[0][0] is None
            commands = {c["name"]: c for c in requests[0][1]}
            assert set(commands) == {"checkbookings", "autobook", "bookings", "watcher", "koppel", "deel"}
            for name in commands:
                assert set(commands[name]["contexts"]) == {0, 1, 2}
                assert set(commands[name]["integration_types"]) == {0, 1}
            day = commands["checkbookings"]["options"][0]
            assert day["name"] == "dag" and day["type"] == discord.AppCommandOptionType.string.value
            assert "0-5" in day["description"]
            assert "2 = overmorgen" in day["description"]
            assert day["required"] is False
            assert [r[0] for r in requests] == ([None, guild_id] if guild_id else [None])
            if guild_id:
                assert {c["name"] for c in requests[1][1]} == {"checkbookings", "autobook", "bookings", "watcher", "koppel", "deel"}
            options = {option["name"]: option for option in commands["watcher"]["options"]}
            assert options["dag"]["min_value"] == 0 and options["dag"]["max_value"] == 365
            assert options["van"]["max_value"] == 23 and options["tot"]["max_value"] == 24
        finally:
            await bot.close()
    asyncio.run(run())


@pytest.mark.parametrize("command,args", [("checkbookings", (0,)), ("autobook", ()), ("bookings", ()), ("watcher", (0, 13, 20))])
def test_other_group_member_cannot_open_private_panels(tmp_path, monkeypatch, command, args):
    monkeypatch.setattr(settings, "padelbot_rules_file", tmp_path / "rules.sqlite3")
    monkeypatch.setattr(settings, "discord_owner_id", 123456789012345679)
    async def run():
        bot = make_bot()
        replies = []
        async def send_message(message, **kwargs): replies.append((message, kwargs))
        interaction = SimpleNamespace(
            guild_id=None, user=SimpleNamespace(id=123456789012345680),
            response=SimpleNamespace(send_message=send_message),
        )
        try:
            await bot.tree.get_command(command).callback(interaction, *args)
            assert len(replies) == 1
            assert "Not authorized" in replies[0][0]
            assert replies[0][1]["ephemeral"] is True
        finally:
            await bot.close()
    asyncio.run(run())


def component_interaction(bot, *, user=42, component_type=2, custom_id="old-cancel"):
    events = []
    done = False
    async def defer(**kwargs):
        nonlocal done
        done = True
        events.append(("defer", kwargs))
    async def send(content, **kwargs):
        nonlocal done
        done = True
        events.append(("send", {"content": content, **kwargs}))
    async def edit(**kwargs):
        events.append(("edit", kwargs))
    return SimpleNamespace(
        type=discord.InteractionType.component, client=bot,
        data={"component_type": component_type, "custom_id": custom_id},
        user=SimpleNamespace(id=user),
        message=SimpleNamespace(id=100, author=bot.user,
            embeds=[discord.Embed(title="Mijn reservaties (2)")]),
        response=SimpleNamespace(is_done=lambda: done, defer=defer, send_message=send, edit_message=edit),
        edit_original_response=edit, events=events,
    )


def recovery_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "padelbot_rules_file", tmp_path / "rules.sqlite3")
    monkeypatch.setattr(settings, "discord_owner_id", 42)
    bot = make_bot()
    bot._connection.user = SimpleNamespace(id=900)
    return bot


@pytest.mark.parametrize("component_type", [2, 3])
def test_old_reservation_buttons_and_selects_recover_without_cancelling(tmp_path, monkeypatch, component_type):
    monkeypatch.setattr(ui, "cancel_booking", lambda *a, **kw: pytest.fail("An expired click must not cancel"))
    monkeypatch.setattr(ui, "publish_bookings", lambda *a, **kw: pytest.fail("An expired click must not publish"))
    async def run():
        bot = recovery_bot(tmp_path, monkeypatch)
        it = component_interaction(bot, component_type=component_type)
        view = ui.BookingsView(42, [], {})
        async def load(owner):
            assert owner == 42
            assert it.events == [("defer", {"ephemeral": True, "thinking": True})]
            return view
        monkeypatch.setattr(ui.BookingsView, "load", load)
        try:
            await bot.on_interaction(it)
            assert it.events[-1][0] == "edit"
            assert it.events[-1][1]["view"] is view
            assert view.selected_ids == ()
            assert "nog niets geannuleerd" in it.events[-1][1]["content"]
        finally:
            await bot.close()
    asyncio.run(run())


@pytest.mark.parametrize("registry_id", [100, None, 200])
def test_live_panel_callbacks_are_not_intercepted(tmp_path, monkeypatch, registry_id):
    monkeypatch.setattr(ui, "fetch_bookings", lambda: pytest.fail("Live callback owns the response"))
    async def run():
        bot = recovery_bot(tmp_path, monkeypatch)
        view = ui.BookingsView(42, [], {})
        button = view.children[0]
        it = component_interaction(bot, custom_id=button.custom_id)
        if registry_id == 200:
            it.message.interaction_metadata = SimpleNamespace(id=200)
        # Exercise the installed SDK's real registry, including global fallback.
        bot._connection.store_view(view, registry_id)
        try:
            await bot.on_interaction(it)
            assert it.events == []
            view.stop()
            async def load(owner): return ui.BookingsView(owner, [], {})
            monkeypatch.setattr(ui.BookingsView, "load", load)
            await bot.on_interaction(it)
            assert it.events[0][0] == "defer"
        finally:
            view.stop()
            await bot.close()
    asyncio.run(run())


def test_expired_panel_denies_other_group_members_before_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "fetch_bookings", lambda: pytest.fail("Private account must not be read"))
    async def run():
        bot = recovery_bot(tmp_path, monkeypatch)
        it = component_interaction(bot, user=43)
        try:
            await bot.on_interaction(it)
            assert len(it.events) == 1
            assert it.events[0][0] == "send"
            assert it.events[0][1]["ephemeral"] is True
            assert "Not authorized" in it.events[0][1]["content"]
        finally:
            await bot.close()
    asyncio.run(run())


@pytest.mark.parametrize('owner',[True,False])
def test_expired_legacy_panel_uses_shared_profile_worker_in_platform_mode(tmp_path,monkeypatch,owner):
    from padelbot import discord_platform
    async def legacy(*args):pytest.fail('Platform recovery must not read the global owner account')
    monkeypatch.setattr(ui.BookingsView,'load',legacy)
    calls=[]
    async def modern(interaction,service,kind):calls.append((interaction.user.id,service,kind))
    monkeypatch.setattr(discord_platform,'command',modern)
    async def run():
        bot=recovery_bot(tmp_path,monkeypatch);bot.platform=object()
        it=component_interaction(bot,user=42 if owner else 43)
        try:
            await bot.on_interaction(it)
            assert calls==([(42,bot.platform,'bookings')] if owner else [])
            if not owner:assert it.events[0][1]['ephemeral'] is True
        finally:await bot.close()
    asyncio.run(run())


@pytest.mark.parametrize("case", ["other-author", "other-panel", "no-message", "slash", "already-replied"])
def test_recovery_is_limited_to_unhandled_bot_reservation_panels(tmp_path, monkeypatch, case):
    monkeypatch.setattr(ui, "fetch_bookings", lambda: pytest.fail("Unrelated interaction must be ignored"))
    async def run():
        bot = recovery_bot(tmp_path, monkeypatch)
        it = component_interaction(bot)
        if case == "other-author": it.message.author = SimpleNamespace(id=901)
        elif case == "other-panel": it.message.embeds = [discord.Embed(title="Padel met de groep")]
        elif case == "no-message": it.message = None
        elif case == "slash": it.type = discord.InteractionType.application_command
        else: it.response.is_done = lambda: True
        try:
            await bot.on_interaction(it)
            assert it.events == []
        finally:
            await bot.close()
    asyncio.run(run())


def test_expired_panel_reports_reload_failure_after_acknowledging(tmp_path, monkeypatch):
    async def failed(owner): raise TimeoutError("private backend detail")
    monkeypatch.setattr(ui.BookingsView, "load", failed)
    async def run():
        bot = recovery_bot(tmp_path, monkeypatch)
        it = component_interaction(bot)
        try:
            await bot.on_interaction(it)
            assert [kind for kind, _ in it.events] == ["defer", "edit"]
            assert it.events[-1][1]["view"] is None
            assert "`/bookings`" in it.events[-1][1]["content"]
            assert "private backend detail" not in it.events[-1][1]["content"]
        finally:
            await bot.close()
    asyncio.run(run())


def test_after_cancellation_new_selection_works_and_old_clicks_recover(tmp_path, monkeypatch):
    writes = []
    mine = [SimpleNamespace(id=bid, start_dt=ui.today(), raw={"productId": 4385}) for bid in (123, 124)]
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def me(self): return {"id": 99}
    def cancel(client, **kwargs):
        writes.append(kwargs)
        return {"statusCode": 2}
    async def shared(*args): pass
    async def load(owner): return ui.BookingsView(owner, mine[1:], {4385: "Padel 4"})
    monkeypatch.setattr(ui, "BackboneClient", Client)
    monkeypatch.setattr(ui, "cancel_booking", cancel)
    monkeypatch.setattr(ui, "cancel_shared_booking", shared)
    monkeypatch.setattr(ui.BookingsView, "load", load)
    async def run():
        bot = recovery_bot(tmp_path, monkeypatch)
        old = ui.BookingsView(42, mine, {4385: "Padel 4"}, selected=123)
        old_ids = [(item.type.value, item.custom_id) for item in old.children]
        bot._connection.store_view(old, 100)
        it = component_interaction(bot)
        views = []
        changed = asyncio.Event()
        async def edit(**kwargs):
            it.events.append(("edit", kwargs))
            view = kwargs.get("view")
            if view is not None:
                # Model the real SDK's edit_original_response registration.
                bot._connection.store_view(view, 100)
                views.append(view)
            changed.set()
        it.edit_original_response = edit
        try:
            await old.cancel(it)
            assert old.is_finished(), "The processed view must no longer handle stale clicks"
            assert not any(key in bot._connection._view_store._views.get(100, {}) for key in old_ids)
            fresh = views[-1]
            select = fresh.children[0]
            next_it = component_interaction(bot, component_type=3, custom_id=select.custom_id)
            next_it.data["values"] = ["124"]
            next_it.response.edit_message = edit
            changed.clear()
            bot._connection._view_store.dispatch_view(3, select.custom_id, next_it)
            await asyncio.wait_for(changed.wait(), timeout=1)
            assert views[-1].selected == 124
            assert next_it.events == [], "Selecting the remaining booking must not be rejected as processed"
            assert fresh.is_finished(), "Replacing a selection must also retire its previous handlers"

            # A late click carrying the old cancelled booking's button must only reload.
            stale = component_interaction(bot, custom_id=old_ids[-1][1])
            await bot.on_interaction(stale)
            assert stale.events[0] == ("defer", {"ephemeral": True, "thinking": True})
            assert "nog niets geannuleerd" in stale.events[-1][1]["content"]
            assert writes == [{"booking_id": 123, "member_id": 99}]
        finally:
            old.stop()
            for view in views: view.stop()
            await bot.close()
    asyncio.run(run())


def test_watcher_command_creates_one_read_only_watch_and_can_list_it(tmp_path, monkeypatch):
    from padelbot import availability_watch as aw
    from padelbot import watch_ui
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime(2030, 6, 5, 12, tzinfo=ZoneInfo("Europe/Brussels"))
    monkeypatch.setattr(aw, "local_now", lambda: now)
    monkeypatch.setattr(watch_ui, "local_now", lambda: now)
    monkeypatch.setattr(ui, "today", lambda: now.replace(hour=0))
    monkeypatch.setattr(aw, "fetch_watch_day", lambda day: ([], {}))
    monkeypatch.setattr(ui, "book", lambda *a, **kw: pytest.fail("Starting a watcher must not reserve or pay"))
    async def run():
        bot = recovery_bot(tmp_path, monkeypatch)
        events = []
        async def defer(**kwargs): events.append(("defer", kwargs))
        async def send(content=None, **kwargs): events.append(("send", {"content": content, **kwargs}))
        it = SimpleNamespace(user=SimpleNamespace(id=42), response=SimpleNamespace(defer=defer), followup=SimpleNamespace(send=send),
            edit_original_response=send, client=bot, channel_id=8, guild_id=None, is_expired=lambda: False,
            is_guild_integration=lambda: False)
        command = bot.tree.get_command("watcher")
        try:
            await command.callback(it, 0, 13, 20)
            assert events[0] == ("defer", {"thinking": True, "ephemeral": True})
            assert "Watcher gestart" in events[-1][1]["content"]
            assert "Geen vrije starttijden" in events[-1][1]["embed"].description
            active = bot.watch_store.list_active(42)
            assert len(active) == 1 and active[0].day == now.date() and active[0].snapshot == {}
            assert active[0].origin_channel_id == 8 and active[0].origin_guild_id is None
            assert bot._watch_interactions[active[0].id]["private"].interaction is it
            await command.callback(it, 0, 13, 20)
            assert len(bot.watch_store.list_active(42)) == 1
            assert "al actief" in events[-1][1]["content"]
            await command.callback(it, None, 13, 20)
            assert "Mijn watchers (1)" in events[-1][1]["embed"].title
            key = (2, f"padel:watch:{active[0].id}:stop")
            assert key in bot._connection._view_store._views[None]
            # Invalid hours are rejected before any extra watch or API access.
            monkeypatch.setattr(aw, "fetch_watch_day", lambda day: pytest.fail("Invalid range must not fetch"))
            await command.callback(it, 1, 20, 13)
            assert len(bot.watch_store.list_active(42)) == 1
            assert "einduur moet later" in events[-1][1]["content"]
        finally:
            await bot.close()
    asyncio.run(run())


def test_public_watch_controls_are_restored_after_bot_restart(tmp_path, monkeypatch):
    from padelbot import availability_watch as aw, watch_ui
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime(2030, 6, 5, 12, tzinfo=ZoneInfo("Europe/Brussels"))
    monkeypatch.setattr(aw, "local_now", lambda: now)
    monkeypatch.setattr(watch_ui, "local_now", lambda: now)
    async def run():
        first = recovery_bot(tmp_path, monkeypatch)
        watch, _ = first.watch_store.create(42, now.date(), 13, 20)
        first.watch_store.begin_publish(watch.id, 42)
        first.watch_store.finish_publish(watch.id, 42, channel_id=8, message_id=100, guild_id=7, live=True)
        await first.close()
        restarted = recovery_bot(tmp_path, monkeypatch)
        async def sync(*, guild=None): return restarted.tree.get_commands(guild=guild)
        monkeypatch.setattr(restarted.tree, "sync", sync)
        try:
            await restarted.setup_hook()
            registry = restarted._connection._view_store._views[None]
            for action in ("private", "public", "stop", "publicrefresh", "manage"):
                assert (2, f"padel:watch:{watch.id}:{action}") in registry
            assert restarted.watch_store.get(watch.id).visibility == "public"
        finally:
            await restarted.close()
    asyncio.run(run())
