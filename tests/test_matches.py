from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import asyncio
from types import SimpleNamespace

import discord
import pytest

from padelbot.matches import MatchStore, MatchError
from padelbot import match_ui
from padelbot import discord_ui


def create(store, booking_id=100):
    return store.create(owner_id=42, booking_id=booking_id,
        start_iso="2030-10-12T11:00:00Z", end_iso="2030-10-12T12:29:00Z",
        court="Padel 3", channel_id=88, guild_id=None)


def test_concurrent_joins_never_exceed_four_players_and_survive_restart(tmp_path):
    store = MatchStore(tmp_path / "rules.sqlite3")
    match = create(store)
    def join(uid):
        try:
            store.change_player(match.id, uid, join=True)
            return True
        except MatchError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(join, range(50, 58))) == 3
    restored = MatchStore(store.path).get(match.id)
    assert len(restored.players) == 4 and restored.players[0] == 42
    with pytest.raises(MatchError, match="al op"):
        store.change_player(match.id, restored.players[1], join=True)
    store.change_player(match.id, restored.players[1], join=False)
    store.change_player(match.id, 99, join=True)
    assert len(store.get(match.id).players) == 4


def test_cancel_and_started_matches_cannot_accept_players(tmp_path, monkeypatch):
    store = MatchStore(tmp_path / "rules.sqlite3")
    match = create(store)
    assert store.cancel_booking(43, match.booking_id) is None
    store.cancel_booking(42, match.booking_id)
    with pytest.raises(MatchError, match="geannuleerd"):
        store.change_player(match.id, 50, join=True)
    other = create(store, 101)
    monkeypatch.setattr("padelbot.matches.time.time", lambda: 2100000000)
    with pytest.raises(MatchError, match="begonnen"):
        store.change_player(other.id, 50, join=True)


def test_same_booking_is_not_published_twice_or_bound_to_another_message(tmp_path):
    store = MatchStore(tmp_path / "rules.sqlite3")
    match = create(store)
    assert create(store).id == match.id
    store.bind_message(match.id, 88, 123)
    assert store.for_booking(42, 100).message_id == 123
    for channel_id, message_id in [(89, 123), (88, 124)]:
        with pytest.raises(MatchError):
            store.bind_message(match.id, channel_id, message_id)


def interaction(store, uid=42, *, channel_id=88, message_id=123):
    edits, replies, defers, views = [], [], [], []
    message = SimpleNamespace(id=message_id, author=SimpleNamespace(id=999),
                              flags=SimpleNamespace(ephemeral=False))
    async def send(content=None, **kwargs):
        replies.append((content, kwargs))
        return message
    async def edit(**kwargs): edits.append(kwargs)
    async def defer(**kwargs): defers.append(kwargs)
    bot = SimpleNamespace(user=SimpleNamespace(id=999), match_store=store,
        _match_publish_lock=asyncio.Lock(), _match_action_lock=asyncio.Lock(),
        _booking_lock=asyncio.Lock(), add_view=lambda *args, **kwargs: views.append((args, kwargs)))
    return SimpleNamespace(user=SimpleNamespace(id=uid, bot=False), channel_id=channel_id,
        guild_id=None, message=message, client=bot, app_permissions=SimpleNamespace(mention_everyone=True),
        response=SimpleNamespace(send_message=send, defer=defer),
        followup=SimpleNamespace(send=send, edit_message=lambda *a, **kw: None),
        edit_original_response=edit, edits=edits, replies=replies, defers=defers, views=views)


def test_group_dm_join_and_leave_updates_original_and_confirms_privately(tmp_path):
    async def run():
        store = MatchStore(tmp_path / "rules.sqlite3")
        match = create(store)
        store.bind_message(match.id, 88, 123)
        view = match_ui.GroupMatchView(store, match, asyncio.Lock())
        assert view.is_persistent()
        it = interaction(store, 50)
        await view.act(it, "join")
        assert store.get(match.id).players == (42, 50)
        assert "<@50>" in it.edits[-1]["embed"].fields[0].value
        assert it.replies[-1][1]["ephemeral"] is True
        assert it.replies[-1][1]["allowed_mentions"].users is False
        await view.act(it, "leave")
        assert store.get(match.id).players == (42,)
        assert "afgemeld" in it.replies[-1][0]
    asyncio.run(run())


@pytest.mark.parametrize("channel_id,message_id", [(89, 123), (88, 124)])
def test_copied_or_other_channel_controls_do_not_modify_players(tmp_path, channel_id, message_id):
    async def run():
        store = MatchStore(tmp_path / "rules.sqlite3")
        match = create(store)
        store.bind_message(match.id, 88, 123)
        view = match_ui.GroupMatchView(store, match, asyncio.Lock())
        it = interaction(store, 50, channel_id=channel_id, message_id=message_id)
        await view.act(it, "join")
        assert store.get(match.id).players == (42,)
        assert it.replies[-1][1]["ephemeral"] is True
    asyncio.run(run())


def test_public_share_is_not_inherited_ephemeral_and_double_click_posts_once(tmp_path, monkeypatch):
    monkeypatch.setattr(match_ui, "shareable_booking", lambda bid: (
        SimpleNamespace(id=bid, start_iso="2030-10-12T11:00:00Z", end_iso="2030-10-12T12:29:00Z"), "Padel 3"))
    async def run():
        store = MatchStore(tmp_path / "rules.sqlite3")
        it = interaction(store)
        await asyncio.gather(match_ui.publish_booking(it, 42, 100), match_ui.publish_booking(it, 42, 100))
        assert it.defers[0] == {"thinking": False}
        public = [r for r in it.replies if r[1].get("ephemeral") is False]
        assert len(public) == 1
        assert public[0][1]["view"].is_persistent()
        assert store.for_booking(42, 100).message_id == 123
        assert len(it.views) == 1
    asyncio.run(run())


def test_only_host_can_publish_and_invalid_booking_never_posts_publicly(tmp_path, monkeypatch):
    def fail(*args): raise MatchError("Niet betaald")
    monkeypatch.setattr(match_ui, "shareable_booking", fail)
    async def run():
        store = MatchStore(tmp_path / "rules.sqlite3")
        it = interaction(store, 50)
        await match_ui.publish_booking(it, 42, 100)
        assert not it.defers
        it = interaction(store)
        await match_ui.publish_booking(it, 42, 100)
        assert all(r[1]["ephemeral"] for r in it.replies)
        assert store.for_booking(42, 100) is None
    asyncio.run(run())


def test_paid_confirmation_and_existing_booking_expose_share_button(monkeypatch):
    monkeypatch.setattr(discord_ui.DaySlotsView, "reserve_now", lambda *a, **kw: SimpleNamespace(paid=True, booking_id=100))
    monkeypatch.setattr(discord_ui, "payment_message", lambda result: "Betaald")
    from tests.test_discord_ui import selection, quote_for, interaction as private_interaction
    async def run():
        chosen = selection()
        view = discord_ui.ConfirmBookingView(chosen, quote_for(chosen))
        it = private_interaction()
        await view.confirm(it)
        assert it.edits[-1]["view"].children[0].label == "Deel met groep"
        mine = [SimpleNamespace(id=100, start_dt=discord_ui.today(), raw={"productId": 4384})]
        selected = discord_ui.BookingsView(42, mine, {4384: "Padel 3"}, selected=100)
        assert "Deel met groep" in [getattr(c, "label", None) for c in selected.children]
    asyncio.run(run())


def test_uncertain_public_send_does_not_get_replayed(tmp_path, monkeypatch):
    monkeypatch.setattr(match_ui, "shareable_booking", lambda bid: (
        SimpleNamespace(id=bid, start_iso="2030-10-12T11:00:00Z", end_iso="2030-10-12T12:29:00Z"), "Padel 3"))
    async def run():
        store = MatchStore(tmp_path / "rules.sqlite3")
        it = interaction(store)
        attempts = []
        original_send = it.followup.send
        async def interrupted(content=None, **kwargs):
            if kwargs.get("ephemeral") is False:
                attempts.append(True)
                raise TimeoutError("response lost")
            return await original_send(content, **kwargs)
        it.followup.send = interrupted
        await match_ui.publish_booking(it, 42, 100)
        await match_ui.publish_booking(it, 42, 100)
        assert attempts == [True]
        match = store.for_booking(42, 100)
        assert match and match.message_id is None
        # A legitimate public component click can recover the missing anchor.
        recovered = interaction(store, 50)
        view = match_ui.GroupMatchView(store, match, asyncio.Lock())
        await view.act(recovered, "join")
        assert store.get(match.id).message_id == 123
        assert store.get(match.id).players == (42, 50)
    asyncio.run(run())


def test_cancelling_booking_closes_public_message_and_blocks_new_players(tmp_path):
    async def run():
        store = MatchStore(tmp_path / "rules.sqlite3")
        match = create(store)
        store.bind_message(match.id, 88, 123)
        it = interaction(store)
        public_edits = []
        async def edit(message_id, **kwargs): public_edits.append((message_id, kwargs))
        it.followup.edit_message = edit
        await match_ui.cancel_shared_booking(it, 42, 100)
        assert store.get(match.id).status == "cancelled"
        assert public_edits[-1][0] == 123
        assert "geannuleerd" in public_edits[-1][1]["embed"].title
        assert all(c.disabled for c in public_edits[-1][1]["view"].children[:2])
        guest = interaction(store, 50)
        view = match_ui.GroupMatchView(store, store.get(match.id), it.client._match_action_lock)
        await view.act(guest, "join")
        assert store.get(match.id).players == (42,)
        assert any("geannuleerd" in (message or "") for message, _ in guest.replies)
    asyncio.run(run())


@pytest.mark.parametrize("sale_owner,status", [(78, 0), (77, 1), (77, 2)])
def test_foreign_unpaid_or_cancelled_sale_cannot_be_shared(monkeypatch, sale_owner, status):
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def me(self): return {"id": 77}
        def get(self, path, params=None): return {"id": 12, "customerPersonId": sale_owner, "statusCode": status}
    monkeypatch.setattr(match_ui, "BackboneClient", Client)
    monkeypatch.setattr(match_ui, "list_my_bookings", lambda *a, **kw: [SimpleNamespace(id=100, sale_id=12)])
    with pytest.raises(MatchError):
        match_ui.shareable_booking(100)


def test_persistent_public_buttons_are_registered_after_bot_restart(tmp_path, monkeypatch):
    from padelbot.discord_bot import make_bot
    from padelbot.config import settings
    monkeypatch.setattr(settings, "padelbot_rules_file", tmp_path / "rules.sqlite3")
    monkeypatch.setattr(settings, "discord_guild_id", 0)
    monkeypatch.setattr(settings, "discord_owner_id", 42)
    store = MatchStore(settings.padelbot_rules_file)
    match = create(store)
    store.bind_message(match.id, 88, 123)
    store.change_player(match.id, 50, join=True)
    async def run():
        bot = make_bot()
        async def sync(**kwargs): return []
        monkeypatch.setattr(bot.tree, "sync", sync)
        try:
            await bot.setup_hook()
            restored = [v for v in bot.persistent_views if isinstance(v, match_ui.GroupMatchView)]
            assert len(restored) == 1
            assert restored[0].match_id == match.id
            assert bot.match_store.get(match.id).players == (42, 50)
        finally:
            await bot.close()
    asyncio.run(run())


def test_everyone_tag_is_an_explicit_public_share_choice(tmp_path, monkeypatch):
    monkeypatch.setattr(match_ui, "shareable_booking", lambda bid: (
        SimpleNamespace(id=bid, start_iso="2030-10-12T11:00:00Z", end_iso="2030-10-12T12:29:00Z"), "Padel 3"))
    async def run():
        store = MatchStore(tmp_path / "rules.sqlite3")
        it = interaction(store)
        await match_ui.publish_booking(it, 42, 100, tag_everyone=True)
        content, kwargs = it.replies[0]
        assert content.startswith("@everyone")
        assert kwargs["ephemeral"] is False
        assert kwargs["allowed_mentions"].everyone is True
        assert kwargs["allowed_mentions"].users is False
        guest = interaction(store, 50)
        view = match_ui.GroupMatchView(store, store.for_booking(42, 100), asyncio.Lock())
        await view.act(guest, "join")
        assert guest.replies[-1][1]["allowed_mentions"].everyone is False
    asyncio.run(run())


def test_everyone_without_permission_and_bot_dm_do_not_publish(tmp_path):
    async def run():
        store = MatchStore(tmp_path / "rules.sqlite3")
        it = interaction(store)
        it.app_permissions.mention_everyone = False
        await match_ui.publish_booking(it, 42, 100, tag_everyone=True)
        assert not it.defers and store.for_booking(42, 100) is None
        it = interaction(store)
        it.context = SimpleNamespace(dm_channel=True)
        await match_ui.publish_booking(it, 42, 100)
        assert not it.defers and store.for_booking(42, 100) is None
        assert "groepschat" in it.replies[-1][0]
    asyncio.run(run())


def mock_shareable_booking(bid):
    return (SimpleNamespace(id=bid, start_iso="2030-10-12T11:00:00Z",
                            end_iso="2030-10-12T12:29:00Z"), "Padel 3")


def unique_public_messages(it):
    original_send = it.followup.send
    async def send(content=None, **kwargs):
        await original_send(content, **kwargs)
        return SimpleNamespace(id=122 + len(it.replies), author=SimpleNamespace(id=999),
                               flags=SimpleNamespace(ephemeral=False))
    it.followup.send = send


def test_batch_share_has_independent_rosters_one_ping_and_no_duplicate_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(match_ui, "shareable_booking", mock_shareable_booking)
    async def run():
        store = MatchStore(tmp_path / "rules.sqlite3")
        it = interaction(store)
        unique_public_messages(it)
        await match_ui.publish_bookings(it, 42, (100, 101, 100), tag_everyone=True)
        assert len(it.replies) == 2 and len(it.edits) == 1
        assert [kw["allowed_mentions"].everyone for _, kw in it.replies] == [True, False]
        assert all(kw["ephemeral"] is False for _, kw in it.replies)
        first, second = (store.for_booking(42, bid) for bid in (100, 101))
        assert first.id != second.id and first.message_id != second.message_id
        await it.replies[0][1]["view"].act(interaction(store, 50, message_id=first.message_id), "join")
        assert store.get(first.id).players == (42, 50)
        assert store.get(second.id).players == (42,)
        await match_ui.publish_bookings(it, 42, (100, 101), tag_everyone=True)
        assert len(it.replies) == 2
        assert "Al gedeeld" in it.edits[-1]["content"]
    asyncio.run(run())


def test_batch_continues_after_invalid_or_existing_booking_and_pings_first_new_post(tmp_path, monkeypatch):
    checked = []
    def shareable(bid):
        checked.append(bid)
        if bid == 101: raise MatchError("Niet betaald")
        return mock_shareable_booking(bid)
    monkeypatch.setattr(match_ui, "shareable_booking", shareable)
    async def run():
        store = MatchStore(tmp_path / "rules.sqlite3")
        old = create(store)
        store.bind_message(old.id, 88, 555)
        it = interaction(store)
        await match_ui.publish_bookings(it, 42, (100, 101, 102), tag_everyone=True)
        assert checked == [101, 102] and len(it.replies) == 1
        assert it.replies[0][1]["allowed_mentions"].everyone is True
        assert "Al gedeeld" in it.edits[-1]["content"]
        assert "Niet betaald" in it.edits[-1]["content"]
        assert store.for_booking(42, 101) is None and store.for_booking(42, 102) is not None
    asyncio.run(run())


def test_five_booking_batch_uses_only_five_followups_and_rejects_larger_or_empty_batches(tmp_path, monkeypatch):
    monkeypatch.setattr(match_ui, "shareable_booking", mock_shareable_booking)
    async def run():
        store = MatchStore(tmp_path / "rules.sqlite3")
        it = interaction(store)
        unique_public_messages(it)
        await match_ui.publish_bookings(it, 42, range(100, 105))
        assert len(it.replies) == 5 and len(it.edits) == 1
        assert len(it.views) == 5
        for ids in ((), range(200, 206)):
            bad = interaction(store)
            await match_ui.publish_bookings(bad, 42, ids)
            assert not bad.defers and len(bad.replies) == 1
            assert bad.replies[0][1]["ephemeral"] is True
    asyncio.run(run())


def test_uncertain_first_batch_post_never_repeats_everyone_ping(tmp_path, monkeypatch):
    monkeypatch.setattr(match_ui, "shareable_booking", mock_shareable_booking)
    async def run():
        store = MatchStore(tmp_path / "rules.sqlite3")
        it = interaction(store)
        original_send = it.followup.send
        attempts = []
        async def send(content=None, **kwargs):
            attempts.append(kwargs["allowed_mentions"].everyone)
            if len(attempts) == 1: raise TimeoutError("Response lost")
            return await original_send(content, **kwargs)
        it.followup.send = send
        await match_ui.publish_bookings(it, 42, (100, 101), tag_everyone=True)
        assert attempts == [True, False]
        assert store.for_booking(42, 100).message_id is None
        assert store.for_booking(42, 101).message_id == 123
        assert "niet bevestigd" in it.edits[-1]["content"]
    asyncio.run(run())
