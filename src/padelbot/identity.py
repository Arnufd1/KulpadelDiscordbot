"""Public avatars and names from verified Google/Discord identities only."""
from urllib.parse import urlsplit
import re
import asyncio
import time


def _https_image(value, hosts):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        return None
    try:
        parts = urlsplit(value)
        if (parts.scheme != 'https' or parts.username or parts.password or parts.port
                or parts.fragment or not hosts(parts.hostname or '')):
            return None
    except ValueError:
        return None
    return value


def google_avatar(value):
    return _https_image(value, lambda host: host.endswith('.googleusercontent.com'))


def discord_image(value):
    result = _https_image(value, lambda host: host in {'cdn.discordapp.com', 'media.discordapp.net'})
    if not result:
        return None
    path = urlsplit(result).path
    return result if re.fullmatch(r'/(?:avatars/[0-9]+|embed/avatars|guilds/[0-9]+/users/[0-9]+/avatars)/[A-Za-z0-9_]+\.(?:png|webp|gif|jpg)', path) else None


def discord_avatar(user_id, avatar=None, discriminator='0'):
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        return None
    if isinstance(avatar, str) and re.fullmatch(r'(?:a_)?[a-f0-9]{32}', avatar):
        return f'https://cdn.discordapp.com/avatars/{user_id}/{avatar}.png?size=128'
    number = int(discriminator) if str(discriminator).isdigit() else 0
    default = number % 5 if number else (user_id >> 22) % 6
    return f'https://cdn.discordapp.com/embed/avatars/{default}.png'


def remember_user(hub, user):
    """Capture a real SDK User/Member, never an ID/name sent by a browser."""
    if not user or getattr(user, 'bot', False):
        return
    avatar = discord_image(str(getattr(getattr(user, 'display_avatar', None), 'url', '')))
    if not avatar:
        avatar = discord_avatar(user.id, getattr(getattr(user, 'avatar', None), 'key', None),
                                getattr(user, 'discriminator', '0'))
    hub.remember_discord(user.id, getattr(user, 'display_name', None) or getattr(user, 'name', 'Discord-speler'), avatar)


def remember_interaction(hub, interaction):
    user = getattr(interaction, 'user', None)
    if not user or getattr(user, 'bot', False):
        return
    remember_user(hub, user)
    guild = getattr(interaction, 'guild', None)
    guild_id = getattr(interaction, 'guild_id', None)
    guild_install = getattr(interaction, 'is_guild_integration', lambda: False)()
    if guild is not None and guild_install:
        hub.remember_guild(guild.id, guild.name)
    if getattr(interaction, 'is_user_integration', lambda: False)():
        hub.observe_context(user.id, 'personal')
    if guild_id:
        hub.observe_context(user.id, 'guild', guild_id=int(guild_id))
    else:
        hub.observe_context(user.id, 'private')


async def refresh_known_discord_users(bot, hub):
    """Bounded public profile reads; never fetch unrelated server members."""
    with hub.matches._connection() as c:
        rows=c.execute('''SELECT identities.id,COALESCE(n.seen_at,0) seen_at FROM (
            SELECT discord_id id FROM hub_profiles WHERE discord_id IS NOT NULL
            UNION SELECT id FROM hub_discord_names
            UNION SELECT user_id id FROM match_players WHERE typeof(user_id)='integer'
            ) identities LEFT JOIN hub_discord_names n ON n.id=identities.id
            WHERE identities.id>0 AND COALESCE(n.seen_at,0)<? ORDER BY seen_at LIMIT 30''',
            (time.time()-21600,)).fetchall()
    for row in rows:
        try:
            user=bot.get_user(row['id']) or await bot.fetch_user(row['id'])
            remember_user(hub,user)
        except Exception as error:
            from loguru import logger
            logger.warning('Discord profile refresh deferred: {}',type(error).__name__)
            if getattr(error,'status',None)==429:break
        await asyncio.sleep(.15)


async def run_identity_refresh(bot, hub):
    while not bot.is_closed():
        try:
            await refresh_known_discord_users(bot,hub)
        except Exception as error:
            from loguru import logger
            logger.warning('Discord identity refresh deferred: {}',type(error).__name__)
        await asyncio.sleep(300)
