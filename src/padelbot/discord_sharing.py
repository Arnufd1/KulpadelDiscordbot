"""Server policy for explicit public shares, independent of booking logic."""
from __future__ import annotations

import asyncio

import discord

from .config import settings


class SharePolicyError(ValueError):
    """A definite pre-send rejection, safe to correct and explicitly retry."""


def installed_guild(interaction):
    """User-installed commands in other guilds keep their personal behavior."""
    guild_id = getattr(interaction, 'guild_id', None)
    get_guild = getattr(getattr(interaction, 'client', None), 'get_guild', None)
    return get_guild(guild_id) if guild_id and get_guild else None


def server_settings_url(guild_id):
    return settings.padelbot_public_url.rstrip('/') + f'/#discord/server/{guild_id}'


def with_server_settings(view, interaction):
    guild = installed_guild(interaction)
    if guild is None:
        return view
    if view is None:
        view = discord.ui.View()
    for child in view.children:
        if getattr(child, 'label', None) == 'Deel in deze chat':
            child.label = 'Deel boeking'
    url = server_settings_url(guild.id)
    if not any(getattr(child, 'url', None) == url for child in view.children):
        view.add_item(discord.ui.Button(label='Serverinstellingen', url=url, emoji='⚙️'))
    return view


def _server_preferences(interaction, guild_id):
    platform = getattr(interaction.client, 'platform', None)
    if platform is None:
        # The legacy, single-owner installation has no shared platform DB.
        return {'share_availability': True, 'share_matches': True, 'share_channel_id': None}
    try:
        from .discord_server_settings import DiscordServerSettings
        return DiscordServerSettings(platform.store).get(guild_id)
    except Exception as error:
        raise SharePolicyError('Serverinstellingen laden lukt niet. Er is niets gedeeld; probeer opnieuw.') from error


async def _destination(interaction, kind, *, expected_channel_id=None, attachment=False, mention_everyone=False):
    guild = installed_guild(interaction)
    if guild is None:
        return None
    preferences = await asyncio.to_thread(_server_preferences, interaction, guild.id)
    key = {'availability': 'share_availability', 'matches': 'share_matches'}[kind]
    if preferences.get(key) is not True:
        subject = 'Vrije momenten delen' if kind == 'availability' else 'Boekingen delen'
        raise SharePolicyError(f'{subject} is uitgeschakeld voor deze server. Bekijk Serverinstellingen.')
    channel_id = int(preferences.get('share_channel_id') or interaction.channel_id)
    if expected_channel_id is not None and channel_id != expected_channel_id:
        raise SharePolicyError('Het deelkanaal is gewijzigd. Er is niets gedeeld; open het overzicht opnieuw.')
    try:
        channel = interaction.client.get_channel(channel_id)
        if channel is None:
            channel = await interaction.client.fetch_channel(channel_id)
        if (getattr(getattr(channel, 'guild', None), 'id', None) != guild.id
                or not callable(getattr(channel, 'send', None))
                or not callable(getattr(channel, 'permissions_for', None))):
            raise SharePolicyError('Het ingestelde deelkanaal is niet beschikbaar in deze server.')
        # Interaction members contain current role IDs. A cached guild member
        # may still hold a role that was just revoked, especially when the
        # configured destination differs from the command's channel.
        actor = interaction.user
        if not isinstance(actor, discord.Member) or actor.guild.id != guild.id:
            actor = await guild.fetch_member(interaction.user.id)
        me = guild.me
        if me is None:
            raise SharePolicyError('De bot is niet beschikbaar in deze server.')
        is_thread = isinstance(channel, discord.Thread)
        if is_thread and (channel.archived or channel.locked):
            raise SharePolicyError('De ingestelde thread is gesloten. Kies een ander deelkanaal.')
        for member in (actor, me):
            permissions = channel.permissions_for(member)
            can_send = getattr(permissions, 'send_messages_in_threads' if is_thread else 'send_messages', False)
            if not (permissions.view_channel and can_send and permissions.embed_links):
                raise SharePolicyError('Jij en de bot moeten het deelkanaal kunnen bekijken en er berichten met embeds sturen.')
            if attachment and not permissions.attach_files:
                raise SharePolicyError('Jij en de bot moeten bestanden kunnen sturen in het deelkanaal.')
        if mention_everyone and not channel.permissions_for(me).mention_everyone:
            raise SharePolicyError('De bot mag geen @everyone taggen in het deelkanaal.')
        return channel
    except discord.HTTPException as error:
        raise SharePolicyError('Het ingestelde deelkanaal is niet beschikbaar of niet toegankelijk. Pas Serverinstellingen aan.') from error


class _ShareFollowup:
    def __init__(self, interaction):
        self.interaction = interaction

    def __getattr__(self, name):
        return getattr(self.interaction.original.followup, name)

    async def send(self, *args, **kwargs):
        proxy = self.interaction
        if kwargs.get('ephemeral') is True:
            return await proxy.original.followup.send(*args, **kwargs)
        mentions = kwargs.get('allowed_mentions')
        channel = await _destination(proxy.original, proxy.kind,
            expected_channel_id=proxy.channel_id,
            attachment=bool(kwargs.get('file') or kwargs.get('files')),
            mention_everyone=bool(mentions and mentions.everyone))
        if channel is None:
            raise SharePolicyError('De bot is niet meer actief in deze server. Open het overzicht opnieuw.')
        # Channel.send is deliberate: application followups cannot target a
        # configured channel. The original ephemeral panel stays private.
        kwargs.pop('ephemeral', None)
        kwargs.pop('wait', None)
        return await channel.send(*args, **kwargs)


class _ShareInteraction:
    def __init__(self, original, channel, kind):
        self.original, self.channel, self.kind = original, channel, kind
        self.channel_id = channel.id
        self.guild_id = channel.guild.id
        self.guild = channel.guild
        self.app_permissions = channel.permissions_for(channel.guild.me)
        self.followup = _ShareFollowup(self)

    def __getattr__(self, name):
        return getattr(self.original, name)


async def prepare_public_share(interaction, kind, *, attachment=False, mention_everyone=False):
    """Resolve/authorize before a latch, roster update or post reservation."""
    if isinstance(interaction, _ShareInteraction):
        await _destination(interaction.original, kind, expected_channel_id=interaction.channel_id,
                           attachment=attachment, mention_everyone=mention_everyone)
        return interaction
    channel = await _destination(interaction, kind, attachment=attachment, mention_everyone=mention_everyone)
    return _ShareInteraction(interaction, channel, kind) if channel is not None else interaction
