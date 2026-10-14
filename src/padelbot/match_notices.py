"""Expire short player announcements without touching the reservation post."""
import asyncio

import discord
from loguru import logger


def can_expire_notices(interaction):
    get_guild = getattr(interaction.client, "get_guild", None)
    guild = get_guild(interaction.guild_id) if get_guild and interaction.guild_id else None
    return guild is not None and guild.me is not None


async def delete_due_notices(bot):
    store = bot.match_store
    for notice in store.due_notices():
        channel_id, message_id = notice["channel_id"], notice["message_id"]
        try:
            channel = bot.get_channel(channel_id) or bot.get_partial_messageable(channel_id)
            await channel.get_partial_message(message_id).delete()
        except discord.NotFound as error:
            if error.code == 10008:
                store.notice_deleted(channel_id, message_id)
            else:
                store.retry_notice(channel_id, message_id)
        except Exception as error:
            logger.warning("Player announcement expiry deferred: {}", type(error).__name__)
            store.retry_notice(channel_id, message_id)
        else:
            store.notice_deleted(channel_id, message_id)


async def run_notice_cleanup(bot):
    while not bot.is_closed():
        try:
            await delete_due_notices(bot)
        except Exception as error:
            logger.warning("Player announcement cleanup failed: {}", type(error).__name__)
        await asyncio.sleep(30)
