import asyncio
import sqlite3
from types import SimpleNamespace

import discord
import pytest

from padelbot import match_ui
from padelbot.matches import MatchStore, MatchError
from tests.test_matches import create, interaction, mock_shareable_booking


@pytest.fixture
def shared(tmp_path, monkeypatch):
    monkeypatch.setattr(match_ui, "shareable_booking", mock_shareable_booking)
    store = MatchStore(tmp_path / "rules.sqlite3")
    match = create(store)
    store.bind_message(match.id, 88, 123)
    store.change_player(match.id, 50, join=True)
    return store, match


def test_migration_preserves_legacy_message_ids_buttons_and_players(tmp_path):
    store = MatchStore(tmp_path / "rules.sqlite3")
    match = create(store)
    store.bind_message(match.id, 88, 123)
    store.change_player(match.id, 50, join=True)
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TABLE shared_match_posts")
    restored = MatchStore(store.path)
    assert restored.get(match.id).players == (42, 50)
    post = restored.get_post(match.id)
    assert (post.channel_id, post.message_id, post.state) == (88, 123, "active")
    async def run():
        view = match_ui.post_view(restored, restored.get(match.id), post, asyncio.Lock())
        assert view.children[0].custom_id == f"padel:match:{match.id}:join"
    asyncio.run(run())


def test_existing_share_offers_both_choices_without_posting(shared):
    async def run():
        store, match = shared
        it = interaction(store, channel_id=89, message_id=124)
        await match_ui.publish_booking(it, 42, 100)
        assert not it.replies
        panel = it.edits[-1]["view"]
        assert isinstance(panel, match_ui.CrossShareView)
        assert "Ook hier delen" in panel.children[0].label
        assert "Verplaatsen naar hier" in panel.children[1].label
        assert panel.children[2].url == store.get_post(match.id).jump_url
        await panel.children[0].callback(it)
        assert len(it.replies) == 1
        assert len(store.posts(match.id, active_only=True)) == 2
        assert store.get(match.id).players == (42, 50)
        assert store.get(match.id).status == "open"
    asyncio.run(run())


def test_five_cross_chat_choices_fit_one_panel(shared):
    async def run():
        store, match = shared
        for bid in range(101, 105):
            other = create(store, bid)
            store.bind_message(other.id, 88, 123 + bid)
        it = interaction(store, channel_id=89)
        await match_ui.publish_bookings(it, 42, range(100, 105))
        panel = it.edits[-1]["view"]
        assert len(panel.children) == 16
        assert max(item.row for item in panel.children) == 4
        assert not it.replies
    asyncio.run(run())


def test_replaced_booking_panel_is_retired_and_back_returns_to_bookings(shared, monkeypatch):
    from padelbot.discord_ui import BookingsView
    from datetime import datetime
    async def run():
        store, match = shared
        mine = [SimpleNamespace(id=100, start_dt=datetime.fromisoformat(match.start_iso.replace("Z", "+00:00")), raw={"productId": 4384})]
        source = BookingsView(42, mine, {4384: "Padel 3"}, selected=100)
        it = interaction(store, channel_id=89, message_id=124)
        await source.share(it)
        assert source.is_finished()
        panel = it.edits[-1]["view"]
        async def load(owner_id): return BookingsView(owner_id, mine, {4384: "Padel 3"})
        monkeypatch.setattr(BookingsView, "load", load)
        await panel.bookings(it)
        assert isinstance(it.edits[-1]["view"], BookingsView)
        assert panel.is_finished()
    asyncio.run(run())


def test_concurrent_copy_posts_once_and_shares_capacity_across_chats(shared):
    async def run():
        store, match = shared
        it = interaction(store, channel_id=89, message_id=124)
        await asyncio.gather(match_ui.transfer_share(it, 42, 100), match_ui.transfer_share(it, 42, 100))
        assert len(it.replies) == 1
        post = store.post_in_channel(match.id, 89)
        assert post.message_id == 124
        view = match_ui.post_view(store, store.get(match.id), post, it.client._match_action_lock)
        await view.act(interaction(store, 51, channel_id=89, message_id=124), "join")
        old = match_ui.GroupMatchView(store, store.get(match.id), it.client._match_action_lock)
        await old.act(interaction(store, 52), "join")
        await view.act(interaction(store, 53, channel_id=89, message_id=124), "join")
        assert store.get(match.id).players == (42, 50, 51, 52)
        await old.act(interaction(store, 51), "leave")
        assert store.get(match.id).players == (42, 50, 52)
    asyncio.run(run())


def test_move_confirms_destination_before_deleting_source_and_preserves_roster(shared):
    async def run():
        store, match = shared
        it = interaction(store, channel_id=89, message_id=124)
        order = []
        send = it.followup.send
        async def publish(*args, **kwargs):
            order.append("publish")
            assert store.get_post(match.id).state == "active"
            return await send(*args, **kwargs)
        async def delete(message_id):
            order.append("delete")
            assert message_id == 123
            assert store.post_in_channel(match.id, 89).message_id == 124
        it.followup.send, it.followup.delete_message = publish, delete
        await match_ui.transfer_share(it, 42, 100, move=True)
        assert order == ["publish", "delete"]
        assert store.get_post(match.id).state == "removed"
        assert store.get(match.id).channel_id == 89
        assert store.get(match.id).players == (42, 50)
        assert store.get(match.id).status == "open"
        assert "verwijderd" in it.edits[-1]["content"]
    asyncio.run(run())


def test_move_to_existing_copy_retires_other_chats_without_new_post(shared):
    async def run():
        store, match = shared
        it = interaction(store, channel_id=89, message_id=124)
        await match_ui.transfer_share(it, 42, 100)
        await match_ui.publish_booking(it, 42, 100)
        panel = it.edits[-1]["view"]
        assert panel.children[0].disabled
        assert not panel.children[1].disabled
        assert panel.children[2].url == store.get_post(match.id).jump_url
        deleted = []
        async def delete(mid): deleted.append(mid)
        it.followup.delete_message = delete
        await panel.children[1].callback(it)
        assert len(it.replies) == 1 and deleted == [123]
        assert len(store.posts(match.id, active_only=True)) == 1
        await match_ui.transfer_share(it, 42, 100, move=True)
        assert deleted == [123]
    asyncio.run(run())


def test_unavailable_group_delete_leaves_retired_source_and_fresh_owner_can_delete(shared):
    async def run():
        store, match = shared
        it = interaction(store, channel_id=89, message_id=124)
        await match_ui.transfer_share(it, 42, 100, move=True)
        source = store.get_post(match.id)
        assert source.state == "retired"
        assert "Vernieuwen → Verwijder dit groepsbericht" in it.edits[-1]["content"]
        old = match_ui.GroupMatchView(store, store.get(match.id), it.client._match_action_lock)
        guest = interaction(store, 51)
        await old.act(guest, "join")
        assert store.get(match.id).players == (42, 50)
        assert all(button.disabled for button in guest.edits[-1]["view"].children[:2])
        assert "verplaatst" in guest.edits[-1]["embed"].title
        assert store.post_in_channel(match.id, 89).jump_url in guest.edits[-1]["embed"].description
        await old.act(guest, "remove")
        assert source == store.get_post(match.id)
        owner = interaction(store)
        deleted = []
        async def delete(mid): deleted.append(mid)
        owner.followup.delete_message = delete
        await old.act(owner, "remove")
        assert deleted == [123] and store.get_post(match.id).state == "removed"
        assert store.get(match.id).status == "open"
        assert store.get(match.id).players == (42, 50)
    asyncio.run(run())


def test_group_cross_chat_unknown_message_is_not_treated_as_deleted(shared):
    async def run():
        store, match = shared
        it = interaction(store, channel_id=89, message_id=124)
        async def missing(mid):
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), {"code": 10008, "message": "Unknown Message"})
        it.followup.delete_message = missing
        await match_ui.transfer_share(it, 42, 100, move=True)
        assert store.get_post(match.id).state == "retired"
        local = interaction(store)
        local.followup.delete_message = missing
        view = match_ui.GroupMatchView(store, store.get(match.id), asyncio.Lock())
        await view.act(local, "remove")
        assert store.get_post(match.id).state == "removed"
    asyncio.run(run())


def test_server_own_message_delete_falls_back_to_bot_channel_api(shared):
    async def run():
        store, match = shared
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE shared_match_posts SET guild_id=77 WHERE id=?", (match.id,))
        it = interaction(store, channel_id=89, message_id=124)
        calls = []
        async def delete(): calls.append("deleted")
        def partial(mid):
            assert mid == 123
            return SimpleNamespace(delete=delete)
        def channel(cid):
            assert cid == 88
            return SimpleNamespace(get_partial_message=partial)
        it.client.get_channel = channel
        await match_ui.transfer_share(it, 42, 100, move=True)
        assert calls == ["deleted"]
        assert store.get_post(match.id).state == "removed"
    asyncio.run(run())


@pytest.mark.parametrize("move", [False, True])
def test_lost_send_response_never_replays_or_deletes_original_and_fresh_click_recovers(shared, move):
    async def run():
        store, match = shared
        it = interaction(store, channel_id=89, message_id=124)
        attempts, deletions = [], []
        async def lost(*args, **kwargs):
            attempts.append(True)
            raise TimeoutError("Response lost after publish")
        async def delete(mid): deletions.append(mid)
        it.followup.send, it.followup.delete_message = lost, delete
        await match_ui.transfer_share(it, 42, 100, move=move)
        await match_ui.transfer_share(it, 42, 100, move=move)
        assert attempts == [True] and not deletions
        assert store.get_post(match.id).state == "active"
        post = store.post_in_channel(match.id, 89)
        assert post.message_id is None
        assert it.views[0][0][0].post_id == post.id
        restored = MatchStore(store.path)
        click = interaction(restored, 51, channel_id=89, message_id=124)
        click.followup.delete_message = delete
        view = match_ui.post_view(restored, restored.get(match.id), restored.get_post(post.id), asyncio.Lock())
        await view.act(click, "join")
        assert restored.get_post(post.id).message_id == 124
        assert restored.get(match.id).players == (42, 50, 51)
        assert deletions == ([123] if move else [])
        assert restored.get_post(match.id).state == ("removed" if move else "active")
    asyncio.run(run())


def test_rejected_destination_removes_pending_only_and_allows_retry(shared):
    async def run():
        store, match = shared
        it = interaction(store, channel_id=89, message_id=124)
        send = it.followup.send
        async def rejected(*args, **kwargs):
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Cannot post")
        it.followup.send = rejected
        await match_ui.transfer_share(it, 42, 100, move=True)
        assert store.post_in_channel(match.id, 89) is None
        assert store.get_post(match.id).state == "active"
        assert store.get(match.id).players == (42, 50)
        it.followup.send = send
        await match_ui.transfer_share(it, 42, 100)
        assert store.post_in_channel(match.id, 89).message_id == 124
    asyncio.run(run())


def test_ephemeral_destination_does_not_close_source(shared):
    async def run():
        store, match = shared
        it = interaction(store, channel_id=89, message_id=124)
        async def edit(**kwargs): pass
        async def private(*args, **kwargs):
            return SimpleNamespace(flags=SimpleNamespace(ephemeral=True), edit=edit)
        it.followup.send = private
        await match_ui.transfer_share(it, 42, 100, move=True)
        assert store.post_in_channel(match.id, 89) is None
        assert store.get_post(match.id).state == "active"
        assert "privé" in it.edits[-1]["content"]
    asyncio.run(run())


def test_removing_last_post_allows_republish_without_losing_roster(shared):
    async def run():
        store, match = shared
        owner = interaction(store)
        async def delete(mid): pass
        owner.followup.delete_message = delete
        view = match_ui.GroupMatchView(store, store.get(match.id), asyncio.Lock())
        await view.act(owner, "remove")
        assert not store.posts(match.id, active_only=True)
        again = interaction(store, message_id=125)
        await match_ui.publish_booking(again, 42, 100)
        post = store.post_in_channel(match.id, 88)
        assert post.id != match.id and post.message_id == 125
        assert store.get(match.id).players == (42, 50)
        await view.act(interaction(store, 51), "join")
        assert store.get(match.id).players == (42, 50)
        restored = MatchStore(store.path)
        assert len(restored.posts(match.id, active_only=True)) == 1
    asyncio.run(run())


@pytest.mark.parametrize("reason", ["other-user", "wrong-channel", "dm", "no-mention", "cancelled", "unpaid"])
def test_invalid_cross_chat_actions_never_post_or_retire_source(shared, monkeypatch, reason):
    async def run():
        store, match = shared
        panel = match_ui.CrossShareView(store, 42, 89, (100,), tag_everyone=True)
        it = interaction(store, uid=51 if reason == "other-user" else 42,
            channel_id=90 if reason == "wrong-channel" else 89, message_id=124)
        if reason == "dm": it.context = SimpleNamespace(dm_channel=True)
        if reason == "no-mention": it.app_permissions.mention_everyone = False
        if reason == "cancelled": store.cancel_booking(42, 100)
        if reason == "unpaid":
            def unpaid(bid): raise MatchError("Betaling nog niet bevestigd")
            monkeypatch.setattr(match_ui, "shareable_booking", unpaid)
        await panel.children[1].callback(it)
        assert not any(kwargs.get("ephemeral") is False for _, kwargs in it.replies)
        assert store.post_in_channel(match.id, 89) is None
        assert store.get_post(match.id).state == "active"
        assert store.get(match.id).players == (42, 50)
    asyncio.run(run())


def test_explicit_mention_option_is_retained_in_copy(shared):
    async def run():
        store, match = shared
        it = interaction(store, channel_id=89, message_id=124)
        await match_ui.publish_booking(it, 42, 100, tag_everyone=True)
        panel = it.edits[-1]["view"]
        assert "@everyone" in panel.children[0].label
        await panel.children[0].callback(it)
        content, kwargs = it.replies[-1]
        assert content.startswith("@everyone") and kwargs["allowed_mentions"].everyone is True
        await panel.children[0].callback(it)
        assert len(it.replies) == 1
    asyncio.run(run())


def test_pending_move_remove_does_not_remove_original_source(shared):
    async def run():
        store, match = shared
        post, _ = store.reserve_post(match.id, 42, 89, None, move=True)
        it = interaction(store, channel_id=89, message_id=124)
        deleted = []
        async def delete(mid): deleted.append(mid)
        it.followup.delete_message = delete
        view = match_ui.post_view(store, store.get(match.id), post, asyncio.Lock())
        await view.act(it, "remove")
        assert deleted == [124]
        assert store.get_post(match.id).state == "active"
    asyncio.run(run())


def test_persistent_restore_registers_every_active_and_retired_post(shared, monkeypatch):
    from padelbot.discord_bot import make_bot
    from padelbot.config import settings
    store, match = shared
    target, _ = store.reserve_post(match.id, 42, 89, None)
    store.bind_post(target.id, 89, 124)
    store.move_posts(match.id, 42, target.id)
    pending, _ = store.reserve_post(match.id, 42, 90, None)
    monkeypatch.setattr(settings, "padelbot_rules_file", store.path)
    monkeypatch.setattr(settings, "discord_guild_id", 0)
    monkeypatch.setattr(settings, "discord_owner_id", 42)
    async def run():
        bot = make_bot()
        async def sync(**kwargs): return []
        monkeypatch.setattr(bot.tree, "sync", sync)
        try:
            await bot.setup_hook()
            restored = [v for v in bot.persistent_views if isinstance(v, match_ui.GroupMatchView)]
            assert {view.post_id for view in restored} == {match.id, target.id, pending.id}
            assert bot.match_store.get(match.id).players == (42, 50)
        finally:
            await bot.close()
    asyncio.run(run())


def test_cancellation_closes_all_reachable_copies_and_stale_group_buttons(shared):
    async def run():
        store, match = shared
        target, _ = store.reserve_post(match.id, 42, 89, 77)
        store.bind_post(target.id, 89, 124)
        it = interaction(store)
        edits = []
        async def edit(mid, **kwargs): edits.append((mid, kwargs))
        async def edit_remote(**kwargs): edits.append((124, kwargs))
        it.followup.edit_message = edit
        it.client.get_channel = lambda cid: SimpleNamespace(get_partial_message=lambda mid: SimpleNamespace(edit=edit_remote))
        await match_ui.cancel_shared_booking(it, 42, 100)
        assert {mid for mid, _ in edits} == {123, 124}
        assert all(all(button.disabled for button in kwargs["view"].children[:2]) for _, kwargs in edits)
        view = match_ui.post_view(store, store.get(match.id), target, asyncio.Lock())
        await view.act(interaction(store, 51, channel_id=89, message_id=124), "join")
        assert store.get(match.id).players == (42, 50)
    asyncio.run(run())
