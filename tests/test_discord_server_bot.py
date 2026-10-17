import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord
import pytest

from padelbot import discord_bot, discord_platform, discord_sharing, discord_ui
from padelbot.discord_server_settings import DiscordServerSettings
from padelbot.rules_store import RulesStore
from test_web import portal


def guild_interaction(service, *, installed=True):
    """No Discord network calls: both source and configured target are fake."""
    replies, edits, messages, views = [], [], [], []
    actor, bot_member = SimpleNamespace(id=42), SimpleNamespace(id=999)
    members = {42: actor, 999: bot_member}
    permissions = {uid: SimpleNamespace(view_channel=True, send_messages=True,
        send_messages_in_threads=True, embed_links=True, attach_files=True,
        mention_everyone=True) for uid in members}
    guild = SimpleNamespace(id=100, me=bot_member, get_member=members.get)
    async def fresh_member(ident):
        return members[ident]
    guild.fetch_member = fresh_member
    channels = {}

    async def public_send(content=None, **kwargs):
        message = SimpleNamespace(id=500 + len(messages), flags=SimpleNamespace(ephemeral=False),
            jump_url=f'https://discord.com/channels/100/90/{500 + len(messages)}')
        messages.append({'content': content, **kwargs})
        return message

    for ident in (88, 90):
        channels[ident] = SimpleNamespace(id=ident, guild=guild, send=public_send,
            permissions_for=lambda member: permissions[member.id])

    async def missing_channel(ident):
        return None

    async def reply(content=None, **kwargs):
        replies.append({'content': content, **kwargs})
        return SimpleNamespace(flags=SimpleNamespace(ephemeral=kwargs.get('ephemeral', False)),
            jump_url='https://discord.com/channels/100/88/400')

    async def edit(**kwargs):
        edits.append(kwargs)

    async def defer(**kwargs):
        pass

    bot = SimpleNamespace(platform=service, user=bot_member, match_store=service.community.matches,
        get_guild=lambda ident: guild if installed and ident == guild.id else None,
        get_channel=channels.get, fetch_channel=missing_channel,
        _match_publish_lock=asyncio.Lock(), _match_action_lock=asyncio.Lock(), _booking_lock=asyncio.Lock(),
        add_view=lambda *args, **kwargs: views.append((args, kwargs)))
    return SimpleNamespace(user=actor, client=bot, guild=guild, guild_id=100, channel_id=88,
        channel=channels[88], app_permissions=permissions[999],
        context=SimpleNamespace(dm_channel=False),
        response=SimpleNamespace(send_message=reply, defer=defer, edit_message=edit),
        followup=SimpleNamespace(send=reply), edit_original_response=edit,
        replies=replies, edits=edits, messages=messages, channels=channels, permissions=permissions)


def linked_service(portal):
    app, _, _, (_, alice, _) = portal
    service = app.state.platform
    service.hub.register(alice)
    service.hub.link(alice['id'], 42)
    return service, alice


def test_settings_command_is_guild_install_only_and_private(tmp_path):
    async def run():
        bot = discord_bot.PadelBot(RulesStore(tmp_path / 'rules.sqlite3'))
        discord_bot._register_commands(bot)
        command = bot.tree.get_command('settings')
        assert command.allowed_installs.guild and not command.allowed_installs.user
        assert command.allowed_contexts.guild and not command.allowed_contexts.dm_channel
        assert not command.allowed_contexts.private_channel
        sent = []

        async def reply(content, **kwargs):
            sent.append((content, kwargs))

        guild = SimpleNamespace(id=100)
        it = SimpleNamespace(guild_id=100, client=SimpleNamespace(get_guild=lambda _: guild),
            response=SimpleNamespace(send_message=reply))
        await command.callback(it)
        assert sent[-1][1]['ephemeral'] is True
        button = sent[-1][1]['view'].children[0]
        assert button.label == 'Serverinstellingen'
        assert button.url.endswith('/#discord/server/100')
        it.guild_id = None
        await command.callback(it)
        assert sent[-1][1]['ephemeral'] is True and 'view' not in sent[-1][1]
        await bot.close()

    asyncio.run(run())


@pytest.mark.parametrize('kind', ['slots', 'bookings', 'rules'])
def test_platform_private_panels_include_current_server_settings(portal, monkeypatch, kind):
    service, _ = linked_service(portal)
    monkeypatch.setattr(discord_platform.weather, 'get_forecast', lambda: None)

    async def queued(*args):
        return {'slots': [], 'bookings': []}

    monkeypatch.setattr(discord_platform, 'job', queued)

    async def run():
        it = guild_interaction(service)
        await discord_platform.command(it, service, kind, 0, 5)
        result = it.replies[-1] if kind == 'rules' else it.edits[-1]
        buttons = [child for child in result['view'].children if getattr(child, 'url', None)]
        assert any(button.url.endswith('/#discord/server/100') for button in buttons)
        assert not it.messages
        if kind == 'slots':
            result['view'].select._values = [result['view'].select.options[0].value]
            await result['view'].choose(it)
            day = it.edits[-1]['view']
            assert any(getattr(child, 'url', '').endswith('/#discord/server/100')
                       for child in day.children if getattr(child, 'url', None))

    asyncio.run(run())


@pytest.mark.parametrize('kind,flag', [('availability', 'share_availability'), ('matches', 'share_matches')])
def test_disabled_sharing_fails_before_any_public_send(portal, kind, flag):
    service, _ = linked_service(portal)
    DiscordServerSettings(service.store).save(100, {flag: False}, 42)

    async def run():
        it = guild_interaction(service)
        with pytest.raises(discord_sharing.SharePolicyError, match='uitgeschakeld'):
            await discord_sharing.prepare_public_share(it, kind)
        assert not it.messages

    asyncio.run(run())


def test_availability_rejection_does_not_consume_share_latch_and_enabled_retry_targets_channel(portal):
    service, _ = linked_service(portal)
    preferences = DiscordServerSettings(service.store)
    preferences.save(100, {'share_availability': False, 'share_channel_id': '90'}, 42)

    async def run():
        it = guild_interaction(service)
        today = discord_platform.today()
        overview = discord_platform.availability_overview([], today, today)
        await discord_ui.share_availability(it, 42, overview)
        assert not overview.share_attempted and not it.messages
        assert it.replies[-1]['ephemeral'] and 'uitgeschakeld' in it.replies[-1]['content']
        preferences.save(100, {'share_availability': True}, 42)
        await discord_ui.share_availability(it, 42, overview)
        assert overview.share_attempted and len(it.messages) == 1
        assert '/90/' in overview.shared_url
        assert it.messages[0]['allowed_mentions'].to_dict() == discord.AllowedMentions.none().to_dict()
        assert 'ephemeral' not in it.messages[0] and 'wait' not in it.messages[0]
        assert all(reply.get('ephemeral') is True for reply in it.replies)

    asyncio.run(run())


@pytest.mark.parametrize('member', [42, 999])
@pytest.mark.parametrize('permission', ['view_channel', 'send_messages', 'embed_links'])
def test_actor_and_bot_must_both_have_destination_permissions(portal, member, permission):
    service, _ = linked_service(portal)
    DiscordServerSettings(service.store).save(100, {'share_channel_id': '90'}, 42)

    async def run():
        it = guild_interaction(service)
        setattr(it.permissions[member], permission, False)
        with pytest.raises(discord_sharing.SharePolicyError, match='Jij en de bot'):
            await discord_sharing.prepare_public_share(it, 'matches')
        assert not it.messages

    asyncio.run(run())


@pytest.mark.parametrize('case', ['deleted', 'foreign-guild', 'attachment', 'everyone'])
def test_invalid_default_destination_never_falls_back_to_source(portal, case):
    service, _ = linked_service(portal)
    DiscordServerSettings(service.store).save(100, {'share_channel_id': '90'}, 42)

    async def run():
        it = guild_interaction(service)
        if case == 'deleted':
            del it.channels[90]
        elif case == 'foreign-guild':
            it.channels[90].guild = SimpleNamespace(id=200)
        elif case == 'attachment':
            it.permissions[999].attach_files = False
        else:
            it.permissions[999].mention_everyone = False
        with pytest.raises(discord_sharing.SharePolicyError):
            await discord_sharing.prepare_public_share(it, 'availability',
                attachment=case == 'attachment', mention_everyone=case == 'everyone')
        assert not it.messages

    asyncio.run(run())


def test_policy_read_failure_blocks_public_shares_only(portal, monkeypatch):
    service, _ = linked_service(portal)

    def broken(*args):
        raise RuntimeError('database unavailable')

    monkeypatch.setattr(DiscordServerSettings, 'get', broken)

    async def run():
        it = guild_interaction(service)
        with pytest.raises(discord_sharing.SharePolicyError, match='Er is niets gedeeld'):
            await discord_sharing.prepare_public_share(it, 'matches')
        view = discord_sharing.with_server_settings(None, it)
        assert view.children[0].url.endswith('/#discord/server/100')
        # A guild interaction from a personal installation is not a server bot.
        it.client.get_guild = lambda _: None
        assert await discord_sharing.prepare_public_share(it, 'matches') is it
        assert discord_sharing.with_server_settings(None, it) is None

    asyncio.run(run())


def test_configured_target_uses_fresh_actor_roles_instead_of_privileged_guild_cache(portal):
    service, _ = linked_service(portal)
    DiscordServerSettings(service.store).save(100, {'share_channel_id': '90'}, 42)

    async def run():
        it = guild_interaction(service)
        old_actor = SimpleNamespace(id=42, privileged=True)
        fresh_actor = SimpleNamespace(id=42, privileged=False)
        it.guild.get_member = lambda _: old_actor
        fetched = []
        async def fresh_member(ident):
            fetched.append(ident)
            return fresh_actor
        it.guild.fetch_member = fresh_member
        def permissions(member):
            result = SimpleNamespace(**vars(it.permissions[member.id]))
            if member.id == 42:
                result.view_channel = member.privileged
            return result
        it.channels[90].permissions_for = permissions
        with pytest.raises(discord_sharing.SharePolicyError, match='Jij en de bot'):
            await discord_sharing.prepare_public_share(it, 'matches')
        assert fetched == [42] and not it.messages

    asyncio.run(run())


def test_fresh_policy_is_checked_again_before_send_and_keeps_private_followups_private(portal):
    service, _ = linked_service(portal)
    preferences = DiscordServerSettings(service.store)
    preferences.save(100, {'share_channel_id': '90'}, 42)

    async def run():
        it = guild_interaction(service)
        prepared = await discord_sharing.prepare_public_share(it, 'matches')
        assert prepared.channel_id == 90
        await prepared.followup.send('private result', ephemeral=True)
        assert it.replies[-1]['ephemeral'] and not it.messages
        preferences.save(100, {'share_matches': False}, 42)
        with pytest.raises(discord_sharing.SharePolicyError, match='uitgeschakeld'):
            await prepared.followup.send('public result', ephemeral=False, wait=True)
        assert not it.messages
        preferences.save(100, {'share_matches': True, 'share_channel_id': None}, 42)
        with pytest.raises(discord_sharing.SharePolicyError, match='gewijzigd'):
            await prepared.followup.send('public result', ephemeral=False, wait=True)
        assert not it.messages

    asyncio.run(run())


def test_availability_policy_changed_during_prepare_is_retryable_without_a_public_send(portal, monkeypatch):
    service, _ = linked_service(portal)
    get = DiscordServerSettings.get
    checks = []

    def switched(self, guild_id):
        checks.append(guild_id)
        result = get(self, guild_id)
        result['share_availability'] = len(checks) == 1
        return result

    monkeypatch.setattr(DiscordServerSettings, 'get', switched)

    async def run():
        it = guild_interaction(service)
        today = discord_platform.today()
        overview = discord_platform.availability_overview([], today, today)
        await discord_ui.share_availability(it, 42, overview)
        assert len(checks) == 2 and not it.messages and not overview.share_attempted
        assert 'uitgeschakeld' in it.replies[-1]['content'] and it.replies[-1]['ephemeral']
        monkeypatch.setattr(DiscordServerSettings, 'get', get)
        await discord_ui.share_availability(it, 42, overview)
        assert len(it.messages) == 1 and overview.shared_url

    asyncio.run(run())


def test_confirmed_match_reservation_and_public_send_use_configured_channel_and_common_roster(portal, monkeypatch):
    service, alice = linked_service(portal)
    DiscordServerSettings(service.store).save(100, {'share_channel_id': '90'}, 42)
    start = datetime.now(timezone.utc) + timedelta(hours=4)
    actual = {'id': 123, 'name': 'Padel 1', 'start': start.isoformat(),
        'end': (start + timedelta(hours=1)).isoformat(), 'confirmed': True}
    jobs = []

    async def fresh(service, user, kind, payload):
        jobs.append((user['id'], kind))
        service.community.sync(user['id'], [actual], device_verified=True)

    monkeypatch.setattr(discord_platform, 'job', fresh)

    async def run():
        it = guild_interaction(service)
        await discord_platform.share_booking(it, service, alice, 123)
        assert jobs == [(alice['id'], 'bookings')]
        assert len(it.messages) == 1 and not it.replies
        match = service.community.matches.for_booking(42, 123)
        post = service.community.matches.post_in_channel(match.id, 90)
        assert post.message_id == 500 and post.guild_id == 100
        assert service.community.matches.post_in_channel(match.id, 88) is None
        assert it.edits[-1]['content'].startswith('Gedeeld.')
        await discord_platform.share_booking(it, service, alice, 123)
        assert len(it.messages) == 1

    asyncio.run(run())


def test_blocked_confirmed_share_never_reads_provider_or_mutates_match(portal, monkeypatch):
    service, alice = linked_service(portal)
    DiscordServerSettings(service.store).save(100, {'share_matches': False}, 42)

    async def forbidden(*args):
        pytest.fail('Disabled sharing should stop before provider checks')

    monkeypatch.setattr(discord_platform, 'job', forbidden)

    async def run():
        it = guild_interaction(service)
        await discord_platform.share_booking(it, service, alice, 123)
        assert service.community.matches.for_booking(42, 123) is None
        assert not it.messages and it.replies[-1]['ephemeral']

    asyncio.run(run())


def test_match_policy_changed_after_reservation_drops_pending_post_for_safe_explicit_retry(portal, monkeypatch):
    service, alice = linked_service(portal)
    get = DiscordServerSettings.get
    checks = []
    start = datetime.now(timezone.utc) + timedelta(hours=4)
    actual = {'id': 123, 'name': 'Padel 1', 'start': start.isoformat(),
        'end': (start + timedelta(hours=1)).isoformat(), 'confirmed': True}

    async def fresh(service, user, kind, payload):
        service.community.sync(user['id'], [actual], device_verified=True)

    def switched(self, guild_id):
        checks.append(guild_id)
        result = get(self, guild_id)
        result['share_matches'] = len(checks) < 3
        return result

    monkeypatch.setattr(discord_platform, 'job', fresh)
    monkeypatch.setattr(DiscordServerSettings, 'get', switched)

    async def run():
        it = guild_interaction(service)
        await discord_platform.share_booking(it, service, alice, 123)
        match = service.community.matches.for_booking(42, 123)
        assert match is not None and not service.community.matches.posts(match.id)
        assert not it.messages and 'uitgeschakeld' in it.edits[-1]['content']
        monkeypatch.setattr(DiscordServerSettings, 'get', get)
        await discord_platform.share_booking(it, service, alice, 123)
        assert len(it.messages) == 1

    asyncio.run(run())
