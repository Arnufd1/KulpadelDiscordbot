"""Refresh existing server posts after web changes; never send new group messages."""
import asyncio,time
import discord
from loguru import logger


async def refresh_hub_posts(bot):
    from .match_ui import match_embed,post_view
    store=bot.match_store
    with store._connection() as c:
        pending=[dict(row) for row in c.execute('SELECT * FROM hub_post_updates WHERE retry_at<=? LIMIT 30',(time.time(),))]
    for update in pending:
        match=store.get(update['match_id'])
        failed=False
        if match:
            for post in store.posts(match.id,active_only=True):
                # User-installed group-DM messages require a fresh interaction webhook.
                # Their Refresh button always reads this same current canonical roster.
                if not post.message_id or not post.guild_id or not bot.get_guild(post.guild_id):continue
                try:
                    channel=bot.get_channel(post.channel_id) or await bot.fetch_channel(post.channel_id)
                    await channel.get_partial_message(post.message_id).edit(embed=match_embed(match),
                        view=post_view(store,match,post,bot._match_action_lock),allowed_mentions=discord.AllowedMentions.none())
                except discord.NotFound:
                    pass
                except Exception as error:
                    failed=True
                    logger.warning('Hub roster refresh deferred: {}',type(error).__name__)
        with store._connection() as c:
            if failed:c.execute('UPDATE hub_post_updates SET retry_at=? WHERE match_id=? AND revision=?',(time.time()+60,update['match_id'],update['revision']))
            else:c.execute('DELETE FROM hub_post_updates WHERE match_id=? AND revision=?',(update['match_id'],update['revision']))


async def run_hub_post_updates(bot):
    while not bot.is_closed():
        try:await refresh_hub_posts(bot)
        except Exception as error:logger.warning('Hub roster refresh failed: {}',type(error).__name__)
        await asyncio.sleep(3)
