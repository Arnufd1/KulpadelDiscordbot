"""Shared server preferences and live Discord authorization for the web portal."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import re
import time

import httpx


ADMINISTRATOR = 1 << 3
MANAGE_GUILD = 1 << 5
VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
EMBED_LINKS = 1 << 14
SHARE_PERMISSIONS = VIEW_CHANNEL | SEND_MESSAGES | EMBED_LINKS
_DEFAULTS = {'share_availability': True, 'share_matches': True, 'share_channel_id': None}


def discord_id(value) -> str:
    """Canonical snowflakes that SQLite and both clients can represent safely."""
    if type(value) not in {int, str}:
        raise ValueError('Ongeldig Discord-ID.')
    value = str(value)
    if not re.fullmatch(r'[1-9][0-9]{0,18}', value) or int(value) >= 2**63:
        raise ValueError('Ongeldig Discord-ID.')
    return value


class DiscordServerSettings:
    def __init__(self, store):
        self.store = store
        with store.conn() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS discord_server_settings (
                guild_id TEXT PRIMARY KEY, share_availability INTEGER NOT NULL DEFAULT 1,
                share_matches INTEGER NOT NULL DEFAULT 1, share_channel_id TEXT,
                updated REAL NOT NULL, updated_by TEXT NOT NULL)''')

    def get(self, guild_id):
        guild_id = discord_id(guild_id)
        with self.store.conn() as connection:
            row = connection.execute('SELECT * FROM discord_server_settings WHERE guild_id=?', (guild_id,)).fetchone()
        if not row:
            return dict(_DEFAULTS)
        return {'share_availability': bool(row['share_availability']), 'share_matches': bool(row['share_matches']),
                'share_channel_id': row['share_channel_id']}

    def save(self, guild_id, values, actor_id):
        """Persist a validated patch. Caller must authorize the actor immediately first."""
        guild_id, actor_id = discord_id(guild_id), discord_id(actor_id)
        if not isinstance(values, dict) or set(values) - set(_DEFAULTS):
            raise ValueError('Ongeldige serverinstellingen.')
        for name in ('share_availability', 'share_matches'):
            if name in values and type(values[name]) is not bool:
                raise ValueError('Ongeldige serverinstellingen.')
        values = dict(values)
        if values.get('share_channel_id') is not None:
            values['share_channel_id'] = discord_id(values['share_channel_id'])
        # Read and merge under the same write lock so independent patches do not
        # replace a concurrent change to a different server preference.
        with self.store.conn() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM discord_server_settings WHERE guild_id=?', (guild_id,)).fetchone()
            current = dict(_DEFAULTS) if row is None else {
                'share_availability': bool(row['share_availability']), 'share_matches': bool(row['share_matches']),
                'share_channel_id': row['share_channel_id']}
            current.update(values)
            connection.execute('''INSERT INTO discord_server_settings VALUES(?,?,?,?,?,?)
                ON CONFLICT(guild_id) DO UPDATE SET share_availability=excluded.share_availability,
                share_matches=excluded.share_matches,share_channel_id=excluded.share_channel_id,
                updated=excluded.updated,updated_by=excluded.updated_by''',
                (guild_id, int(current['share_availability']), int(current['share_matches']),
                 current['share_channel_id'], time.time(), actor_id))
            connection.execute('INSERT INTO audit(actor,action,target,created) VALUES(?,?,?,?)',
                               (actor_id, 'discord_server_settings_saved', guild_id, time.time()))
        return current


class DiscordServerAccessError(Exception):
    def __init__(self, status_code, message):
        self.status_code = status_code
        super().__init__(message)


def _permissions(value):
    if type(value) not in {int, str} or not str(value).isdigit() or len(str(value)) > 30:
        raise ValueError('Invalid permission bitfield')
    return int(value)


def base_permissions(guild, member, roles):
    member_id = discord_id(member['user']['id'])
    if member_id == discord_id(guild['owner_id']):
        return ADMINISTRATOR
    role_ids = {discord_id(value) for value in member['roles']}
    role_map = {discord_id(role['id']): _permissions(role['permissions']) for role in roles}
    guild_id = discord_id(guild['id'])
    if guild_id not in role_map or not role_ids <= role_map.keys():
        raise ValueError('Missing Discord roles')
    permissions = role_map[guild_id]
    for role_id in role_ids:
        permissions |= role_map[role_id]
    return permissions


def channel_permissions(guild, member, roles, channel):
    """Discord order: everyone, combined role denies/allows, then member overwrite."""
    permissions = base_permissions(guild, member, roles)
    if permissions & ADMINISTRATOR:
        return SHARE_PERMISSIONS | ADMINISTRATOR
    role_ids = {discord_id(value) for value in member['roles']}
    overwrites = channel.get('permission_overwrites', [])
    if not isinstance(overwrites, list):
        raise ValueError('Invalid channel overwrites')
    parsed = []
    for overwrite in overwrites:
        if type(overwrite['type']) is not int or overwrite['type'] not in {0, 1}:
            raise ValueError('Invalid channel overwrite type')
        parsed.append((discord_id(overwrite['id']), overwrite['type'],
                       _permissions(overwrite['deny']), _permissions(overwrite['allow'])))
    for target, kind, deny, allow in parsed:
        if kind == 0 and target == discord_id(guild['id']):
            permissions = (permissions & ~deny) | allow
    deny_roles = allow_roles = 0
    for target, kind, deny, allow in parsed:
        if kind == 0 and target in role_ids:
            deny_roles |= deny
            allow_roles |= allow
    permissions = (permissions & ~deny_roles) | allow_roles
    for target, kind, deny, allow in parsed:
        if kind == 1 and target == discord_id(member['user']['id']):
            permissions = (permissions & ~deny) | allow
    # Timed-out members cannot send, even if a role/overwrite allows it.
    if member.get('communication_disabled_until'):
        until = datetime.fromisoformat(member['communication_disabled_until'].replace('Z', '+00:00'))
        if until.tzinfo is None or until > datetime.now(timezone.utc):
            permissions &= ~(SEND_MESSAGES | EMBED_LINKS)
    return permissions


class DiscordServerAccess:
    """Use the bot's REST identity, never client-provided roles or an OAuth cache."""
    def __init__(self, bot_token=None):
        self.bot_token = bot_token

    async def verify(self, guild_id, actor_id):
        guild_id, actor_id = discord_id(guild_id), discord_id(actor_id)
        from .config import settings
        token = settings.discord_bot_token if self.bot_token is None else self.bot_token
        if not token:
            raise DiscordServerAccessError(503, 'Discord is even niet bereikbaar. Probeer opnieuw.')
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False,
                                         headers={'Authorization': 'Bot ' + token}) as client:
                async def get(path):
                    response = await client.get('https://discord.com/api/v10' + path)
                    if response.status_code in {401, 403, 404}:
                        raise DiscordServerAccessError(403, 'Je hebt geen beheerrechten voor deze Discord-server, of de bot is er niet toegevoegd.')
                    response.raise_for_status()
                    return response.json()
                guild, actor, roles, bot = await asyncio.gather(
                    get('/guilds/' + guild_id), get(f'/guilds/{guild_id}/members/{actor_id}'),
                    get(f'/guilds/{guild_id}/roles'), get('/users/@me'))
                if (discord_id(guild['id']) != guild_id or discord_id(actor['user']['id']) != actor_id
                        or not bot.get('bot') or not isinstance(roles, list)):
                    raise ValueError('Discord identity mismatch')
                if not base_permissions(guild, actor, roles) & (ADMINISTRATOR | MANAGE_GUILD):
                    raise DiscordServerAccessError(403, 'Je hebt Server beheren nodig om deze instellingen aan te passen.')
                bot_id = discord_id(bot['id'])
                bot_member, channels = await asyncio.gather(
                    get(f'/guilds/{guild_id}/members/{bot_id}'), get(f'/guilds/{guild_id}/channels'))
                if discord_id(bot_member['user']['id']) != bot_id or not isinstance(channels, list):
                    raise ValueError('Discord bot membership mismatch')
                visible = []
                for channel in channels:
                    if channel.get('type') not in {0, 5} or type(channel.get('type')) is not int:
                        continue
                    if channel.get('guild_id') is not None and discord_id(channel['guild_id']) != guild_id:
                        raise ValueError('Discord channel guild mismatch')
                    channel_id = discord_id(channel['id'])
                    if (channel_permissions(guild, actor, roles, channel) & SHARE_PERMISSIONS == SHARE_PERMISSIONS
                            and channel_permissions(guild, bot_member, roles, channel) & SHARE_PERMISSIONS == SHARE_PERMISSIONS):
                        visible.append({'id': channel_id, 'name': str(channel['name'])[:100]})
                return {'guild': {'id': guild_id, 'name': str(guild['name'])[:100]}, 'channels': visible}
        except DiscordServerAccessError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
            raise DiscordServerAccessError(503, 'Discord-rechten konden niet gecontroleerd worden. Probeer opnieuw.') from None
