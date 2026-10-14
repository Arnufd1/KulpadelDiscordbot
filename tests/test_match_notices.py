import asyncio
from types import SimpleNamespace

import discord
import pytest

from padelbot.matches import MatchStore, MatchError
from padelbot import match_notices, match_ui
from tests.test_matches import interaction


@pytest.fixture
def server_match(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("padelbot.matches.time.time", lambda: clock[0])
    store = MatchStore(tmp_path / "rules.sqlite3")
    match = store.create(owner_id=42, booking_id=100, start_iso="2030-10-12T11:00:00Z",
        end_iso="2030-10-12T12:29:00Z", court="Padel 3", channel_id=88, guild_id=77)
    store.bind_message(match.id, 88, 123)
    store.change_player(match.id, 50, join=True)
    return store, match, clock


def cleaner_bot(store, delete):
    return SimpleNamespace(match_store=store, get_channel=lambda cid: None,
        get_partial_messageable=lambda cid: SimpleNamespace(
            get_partial_message=lambda mid: SimpleNamespace(delete=lambda: delete(cid, mid))))


def test_notice_expires_after_hour_including_restart_and_original_stays(server_match):
    async def run():
        store, match, clock = server_match
        store.queue_notice(match.id, 88, 124)
        store.queue_notice(match.id, 88, 124)
        calls = []
        async def delete(cid, mid): calls.append((cid, mid))
        bot = cleaner_bot(MatchStore(store.path), delete)
        clock[0] = 4599.0
        await match_notices.delete_due_notices(bot)
        assert calls == []
        clock[0] = 4600.0
        await match_notices.delete_due_notices(bot)
        assert calls == [(88, 124)]
        await match_notices.delete_due_notices(bot)
        assert calls == [(88, 124)]
        assert store.get_post(match.id).message_id == 123
        assert store.get(match.id).status == "open"
        assert store.get(match.id).players == (42, 50)
    asyncio.run(run())


def test_player_announcement_is_queued_with_independent_message_id(server_match):
    async def run():
        store, match, clock = server_match
        it = interaction(store, 51)
        it.guild_id = 77
        it.client.get_guild = lambda gid: SimpleNamespace(me=SimpleNamespace(id=999))
        send = it.followup.send
        async def notice(*args, **kwargs):
            await send(*args, **kwargs)
            return SimpleNamespace(id=124, flags=SimpleNamespace(ephemeral=False))
        it.followup.send = notice
        view = match_ui.GroupMatchView(store, store.get(match.id), asyncio.Lock())
        await view.act(it, "join")
        assert store.get(match.id).players == (42, 50, 51)
        assert not store.due_notices()
        clock[0] += 3600
        due = store.due_notices()
        assert [(item["channel_id"], item["message_id"]) for item in due] == [(88, 124)]
        assert it.replies[-1][1]["allowed_mentions"].everyone is False
    asyncio.run(run())


@pytest.mark.parametrize("message_id", [123, 125])
def test_any_original_share_is_protected_from_expiry(server_match, message_id):
    store, match, clock = server_match
    if message_id == 125:
        other = store.create(owner_id=42, booking_id=101, start_iso=match.start_iso,
            end_iso=match.end_iso, court="Padel 4", channel_id=88, guild_id=77)
        store.bind_message(other.id, 88, 125)
    with pytest.raises(MatchError, match="oorspronkelijke"):
        store.queue_notice(match.id, 88, message_id)
    clock[0] += 3600
    assert not store.due_notices()


def test_notice_queue_rejects_other_channel_and_group_dm(server_match):
    store, match, clock = server_match
    with pytest.raises(MatchError): store.queue_notice(match.id, 89, 124)
    target, _ = store.reserve_post(match.id, 42, 89, None)
    store.bind_post(target.id, 89, 125)
    with pytest.raises(MatchError): store.queue_notice(target.id, 89, 126)


def test_missing_notice_is_acknowledged_without_deleting_original(server_match):
    async def run():
        store, match, clock = server_match
        store.queue_notice(match.id, 88, 124)
        clock[0] += 3600
        async def delete(cid, mid):
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), {"code": 10008, "message": "Unknown Message"})
        await match_notices.delete_due_notices(cleaner_bot(store, delete))
        assert not store.due_notices()
        assert store.get_post(match.id).state == "active"
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["timeout", "forbidden", "unknown-channel"])
def test_unconfirmed_delete_remains_persisted_and_retries_with_backoff(server_match, failure):
    async def run():
        store, match, clock = server_match
        store.queue_notice(match.id, 88, 124)
        clock[0] += 3600
        calls = []
        async def delete(cid, mid):
            calls.append(mid)
            if failure == "timeout": raise TimeoutError()
            if failure == "forbidden":
                raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), {"code": 50001, "message": "Missing Access"})
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), {"code": 10003, "message": "Unknown Channel"})
        bot = cleaner_bot(store, delete)
        await match_notices.delete_due_notices(bot)
        await match_notices.delete_due_notices(bot)
        assert calls == [124]
        bot.match_store = MatchStore(store.path)
        clock[0] += 60
        assert len(bot.match_store.due_notices()) == 1
        async def success(cid, mid): calls.append(mid)
        bot.get_partial_messageable = cleaner_bot(store, success).get_partial_messageable
        await match_notices.delete_due_notices(bot)
        assert calls == [124, 124]
        assert not store.due_notices()
        assert store.get(match.id).players == (42, 50)
    asyncio.run(run())


def test_hourly_notice_requires_bot_membership_in_server(server_match):
    store, match, clock = server_match
    it = interaction(store)
    assert not match_notices.can_expire_notices(it)
    it.guild_id = 77
    it.client.get_guild = lambda gid: None
    assert not match_notices.can_expire_notices(it)
    it.client.get_guild = lambda gid: SimpleNamespace(me=SimpleNamespace(id=999))
    assert match_notices.can_expire_notices(it)
