"""Share one reservation across chats while maintaining a single player list."""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from loguru import logger

from .booking import get_product_names
from .booking_manager import list_my_bookings
from .client import BackboneClient
from .config import settings
from .matches import Match, MatchError, MatchStore
from .match_notices import can_expire_notices
from . import weather


def shareable_booking(booking_id):
    with BackboneClient() as client:
        me = client.me()
        booking = next((b for b in list_my_bookings(client, member_id=me["id"]) if b.id == booking_id), None)
        if booking is None or not booking.sale_id:
            raise MatchError("Deze reservatie is niet meer actief in je eigen boekingen.")
        sale = client.get(f"/sales/{booking.sale_id}", params={"s": {"id": booking.sale_id, "customerPersonId": me["id"]}})
        if sale.get("id") != booking.sale_id or sale.get("customerPersonId") != me["id"] or sale.get("statusCode") != 0:
            raise MatchError("Deel de reservatie zodra de betaling door KUL bevestigd is.")
        names = get_product_names(client, {booking.raw["productId"]})
        weather.get_forecast()
        return booking, names.get(booking.raw["productId"], "Padelveld")


def match_embed(match: Match, post=None, store=None):
    tz = ZoneInfo(settings.padel_local_tz)
    start = datetime.fromisoformat(match.start_iso.replace("Z", "+00:00")).astimezone(tz)
    closed = match.status != "open" or match.started
    title = "Padel · geannuleerd" if match.status != "open" else "✅ Padel geboekt · wie speelt mee?"
    if match.started and match.status == "open":
        title = "Padel · gestart"
    body = f"**{start:%d/%m/%Y} · {start:%H:%M} · {discord.utils.escape_markdown(match.court)}**\nOrganisator: <@{match.owner_id}>"
    forecast = weather.peek_forecast()
    if forecast and (badge := forecast.badge(start)):
        body += f"\nLeuven {badge}"
    embed = discord.Embed(title=title, description=body, color=0x888888 if closed else 0x377DFF)
    if post is not None and post.state != "active":
        target = store.get_post(post.replacement_id) if store and post.replacement_id else None
        embed.title = "Padel · verplaatst" if target else "Padel · groepsbericht gesloten"
        embed.description += f"\n\nVerplaatst naar: {target.jump_url}" if target and target.jump_url else "\n\nAanmelden via dit bericht is gesloten. De KUL-reservatie blijft behouden."
        closed = True
    embed.add_field(name=f"Spelers ({len(match.players)}/{match.capacity})", value="\n".join(f"<@{uid}>" for uid in match.players), inline=False)
    footer = "Aanmelden gesloten" if closed else "Volzet" if len(match.players) == match.capacity else f"Nog {match.capacity - len(match.players)} plaatsen · klik Aansluiten"
    embed.set_footer(text=footer + (" · " + weather.CREDIT if forecast else ""))
    return embed


class GroupMatchView(discord.ui.View):
    def __init__(self, store: MatchStore, match: Match, lock: asyncio.Lock, *, post_id=None):
        super().__init__(timeout=None)
        self.store, self.match_id, self.lock = store, match.id, lock
        self.post_id = post_id or match.id
        post = store.get_post(self.post_id)
        prefix = f"padel:post:{self.post_id}" if post_id is not None else f"padel:match:{match.id}"
        closed = match.status != "open" or match.started or post is None or post.state != "active"
        for action, label, style, disabled in [
            ("join", "Aansluiten", discord.ButtonStyle.success, closed or len(match.players) >= match.capacity),
            ("leave", "Afmelden", discord.ButtonStyle.secondary, closed),
            ("refresh", "Vernieuwen", discord.ButtonStyle.secondary, False),
        ]:
            button = discord.ui.Button(label=label, style=style, custom_id=f"{prefix}:{action}", disabled=disabled)
            async def callback(interaction, action=action):
                await self.act(interaction, action)
            button.callback = callback
            self.add_item(button)
        remove = discord.ui.Button(label="Verwijder dit groepsbericht", style=discord.ButtonStyle.danger,
            custom_id=f"{prefix}:remove", row=1, disabled=post is None or post.state == "removed")
        async def remove_callback(interaction):
            await self.act(interaction, "remove")
        remove.callback = remove_callback
        self.add_item(remove)

    async def interaction_check(self, interaction):
        match = self.store.get(self.match_id)
        post = self.store.get_post(self.post_id)
        message = interaction.message
        if (match is None or post is None or interaction.channel_id != post.channel_id or message is None
                or message.author.id != interaction.client.user.id
                or (post.message_id is not None and message.id != post.message_id)
                or message.flags.ephemeral or interaction.user.bot):
            await interaction.response.send_message("Gebruik de knoppen op het oorspronkelijke groepsbericht.", ephemeral=True)
            return False
        return True

    async def act(self, interaction, action):
        # Also validate here for direct callback callers and fresh dispatches.
        if not await self.interaction_check(interaction):
            return
        if action == "remove" and interaction.user.id != self.store.get(self.match_id).owner_id:
            await interaction.response.send_message("Alleen de organisator kan dit groepsbericht verwijderen.", ephemeral=True)
            return
        await interaction.response.defer()
        async with self.lock:
            try:
                post = await asyncio.to_thread(self.store.bind_post, self.post_id, interaction.channel_id, interaction.message.id)
                match = self.store.get(self.match_id)
                if post.move_requested and action != "remove" and match.status == "open" and not match.started:
                    retired = self.store.move_posts(match.id, match.owner_id, post.id)
                    await cleanup_posts(interaction, match, retired)
                    post, match = self.store.get_post(post.id), self.store.get(match.id)
                if action == "remove":
                    self.store.retire_post(post.id, match.owner_id)
                    if await delete_post_message(interaction, post):
                        self.store.retire_post(post.id, match.owner_id, removed=True)
                        await interaction.followup.send("✅ Groepsbericht verwijderd. De reservatie en deelnemerslijst blijven behouden.", ephemeral=True)
                    else:
                        await self.update(interaction, self.store.get(match.id))
                        await interaction.followup.send("Verwijderen niet bevestigd. Dit bericht is gesloten; probeer de verwijderknop opnieuw.", ephemeral=True)
                    return
                if post.state != "active":
                    await self.update(interaction, match)
                    if action != "refresh":
                        await interaction.followup.send("Dit groepsbericht is gesloten of verplaatst. Gebruik het nieuwe bericht.", ephemeral=True)
                    return
                if action in ("join", "leave"):
                    match = await asyncio.to_thread(self.store.change_player, self.match_id, interaction.user.id, join=action == "join")
            except MatchError as error:
                await interaction.followup.send(str(error), ephemeral=True)
                match = self.store.get(self.match_id)
                if match:
                    await self.update(interaction, match)
                return
            await self.update(interaction, match)
            await sync_other_posts(interaction, match, exclude_post_id=self.post_id)
            if action in ("join", "leave"):
                public_notice = can_expire_notices(interaction)
                if public_notice:
                    notice = f"✅ <@{interaction.user.id}> speelt mee" if action == "join" else f"↩️ <@{interaction.user.id}> heeft zich afgemeld"
                else:
                    notice = "✅ Je speelt mee" if action == "join" else "↩️ Je bent afgemeld"
                try:
                    message = await interaction.followup.send(f"{notice} · {len(match.players)}/{match.capacity} spelers.",
                        ephemeral=not public_notice, allowed_mentions=discord.AllowedMentions.none())
                    if public_notice and not message.flags.ephemeral:
                        self.store.queue_notice(self.post_id, interaction.channel_id, message.id)
                except Exception as error:
                    logger.warning("Player announcement failed: {}", type(error).__name__)

    async def update(self, interaction, match):
        post = self.store.get_post(self.post_id)
        view = post_view(self.store, match, post, self.lock)
        try:
            await interaction.edit_original_response(content=None, embed=match_embed(match, post, self.store), view=view, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            await interaction.followup.send("Je keuze is opgeslagen. Het groepsbericht bijwerken lukt nog niet; klik op Vernieuwen.", ephemeral=True)


def post_view(store, match, post, lock):
    return GroupMatchView(store, match, lock, post_id=None if post.id == match.id else post.id)


async def delete_post_message(interaction, post):
    if post.message_id is None:
        return False
    # Try the fresh application webhook first. Some destinations require a
    # component click in the original chat; never treat a cross-chat 404 as
    # proof that the original message disappeared.
    try:
        await interaction.followup.delete_message(post.message_id)
        return True
    except discord.NotFound as error:
        if interaction.channel_id == post.channel_id and error.code == 10008:
            return True
    except Exception as error:
        logger.warning("Shared message webhook deletion unconfirmed: {}", type(error).__name__)
    if post.guild_id:
        try:
            bot = interaction.client
            channel = bot.get_channel(post.channel_id) or await bot.fetch_channel(post.channel_id)
            await channel.get_partial_message(post.message_id).delete()
            return True
        except discord.NotFound as error:
            if error.code == 10008:
                return True
        except Exception as error:
            logger.warning("Shared message channel deletion unconfirmed: {}", type(error).__name__)
    return False


async def cleanup_posts(interaction, match, posts):
    pending = []
    for post in posts:
        if await delete_post_message(interaction, post):
            interaction.client.match_store.retire_post(post.id, match.owner_id, removed=True)
        else:
            pending.append(post)
    return pending


async def sync_other_posts(interaction, match, *, exclude_post_id=None):
    bot, store = interaction.client, interaction.client.match_store
    for post in store.posts(match.id, active_only=True):
        if post.id == exclude_post_id or post.message_id is None:
            continue
        kwargs = dict(content=None, embed=match_embed(match), view=post_view(store, match, post, bot._match_action_lock),
            allowed_mentions=discord.AllowedMentions.none())
        try:
            if interaction.channel_id == post.channel_id:
                await interaction.followup.edit_message(post.message_id, **kwargs)
            elif post.guild_id:
                channel = bot.get_channel(post.channel_id) or await bot.fetch_channel(post.channel_id)
                await channel.get_partial_message(post.message_id).edit(**kwargs)
        except Exception as error:
            # Other group DMs get the current canonical roster on their next click.
            logger.warning("Cross-chat roster update unconfirmed: {}", type(error).__name__)


MAX_SHARE_BOOKINGS = 5


async def _publish_one(interaction, owner_id: int, booking_id: int, *, tag_everyone=False):
    bot = interaction.client
    existing = bot.match_store.for_booking(owner_id, booking_id)
    if existing:
        current = bot.match_store.post_in_channel(existing.id, interaction.channel_id)
        if current:
            if not current.jump_url:
                return False, "Delen nog niet bevestigd; controleer deze chat en klik op Vernieuwen."
            status = f"Al gedeeld in deze chat: {current.jump_url}"
            if any(post.channel_id != interaction.channel_id for post in bot.match_store.posts(existing.id, active_only=True)):
                status += ". Ook elders gedeeld; kies Verplaatsen naar hier om die berichten te verwijderen."
            return False, status
        other = bot.match_store.posts(existing.id, active_only=True)
        if other:
            links = " · ".join(post.jump_url for post in other if post.jump_url)
            return False, f"Al gedeeld in een andere chat: {links or 'bericht nog niet bevestigd'}. Kies hieronder ook hier delen of verplaatsen."
    send_attempted = False
    match, post, created_match = existing, None, False
    try:
        booking, court = await asyncio.to_thread(shareable_booking, booking_id)
        if match is None:
            match = bot.match_store.create(owner_id=owner_id, booking_id=booking.id,
                start_iso=booking.start_iso, end_iso=booking.end_iso, court=court,
                channel_id=interaction.channel_id, guild_id=interaction.guild_id)
            created_match = True
            post = bot.match_store.get_post(match.id)
        else:
            post, _ = bot.match_store.reserve_post(match.id, owner_id, interaction.channel_id, interaction.guild_id)
        send_attempted = True
        post = await send_post(interaction, match, post, tag_everyone=tag_everyone)
        return True, f"Gedeeld: {post.jump_url}"
    except MatchError as error:
        if post and bot.match_store.get_post(post.id) is None and created_match:
            bot.match_store.drop_unpublished(match.id)
            send_attempted = False
        return send_attempted, str(error)
    except discord.HTTPException as error:
        if error.status in (400, 403, 404):
            if post:
                bot.match_store.drop_pending_post(post.id)
            if created_match:
                bot.match_store.drop_unpublished(match.id)
            send_attempted = False
        logger.warning("Sharing booking failed: {}", type(error).__name__)
        return send_attempted, "Delen niet bevestigd; controleer de chat voordat je opnieuw probeert."
    except Exception as error:
        logger.warning("Sharing booking failed: {}", type(error).__name__)
        # A lost send response may already have pinged the group. Do not ping
        # again on the next booking in this batch.
        return send_attempted, "Delen niet bevestigd; controleer de chat voordat je opnieuw probeert."


async def send_post(interaction, match, post, *, tag_everyone=False):
    bot, store = interaction.client, interaction.client.match_store
    view = post_view(store, match, post, bot._match_action_lock)
    # Register before sending: a response can be lost after Discord published
    # the message. Its first legitimate click then binds the unknown message.
    bot.add_view(view)
    message = await interaction.followup.send(content="@everyone 🎾 Wie speelt mee?" if tag_everyone else None,
        embed=match_embed(match), view=view, ephemeral=False, wait=True,
        allowed_mentions=discord.AllowedMentions(everyone=tag_everyone, users=False, roles=False, replied_user=False))
    if message.flags.ephemeral:
        store.drop_pending_post(post.id)
        await message.edit(content="Discord staat hier alleen privé-antwoorden toe. Gebruik een chat waar de app openbaar mag posten.", embed=None, view=None)
        raise MatchError("Discord staat in deze chat alleen privé-antwoorden toe.")
    return store.bind_post(post.id, interaction.channel_id, message.id)


class CrossShareView(discord.ui.View):
    def __init__(self, store, owner_id, channel_id, booking_ids, *, tag_everyone=False):
        super().__init__(timeout=900)
        self.owner_id, self.channel_id = owner_id, channel_id
        self.busy = False
        for row, booking_id in enumerate(booking_ids):
            match = store.for_booking(owner_id, booking_id)
            current = store.post_in_channel(match.id, channel_id)
            start = datetime.fromisoformat(match.start_iso.replace("Z", "+00:00")).astimezone(ZoneInfo(settings.padel_local_tz))
            for move, label in [(False, "Ook hier delen"), (True, "Verplaatsen naar hier")]:
                button = discord.ui.Button(label=f"{start:%d/%m %H:%M} · {label}" + (" + @everyone" if tag_everyone else ""),
                    style=discord.ButtonStyle.primary if not move else discord.ButtonStyle.secondary, row=row,
                    disabled=not move and current is not None and current.message_id is not None)
                async def callback(interaction, booking_id=booking_id, move=move):
                    if self.busy:
                        await interaction.response.send_message("Delen wordt al verwerkt. Wacht even.", ephemeral=True)
                        return
                    self.busy = True
                    try:
                        await transfer_share(interaction, self.owner_id, booking_id, move=move,
                            tag_everyone=tag_everyone, panel=self)
                    finally:
                        self.busy = False
                button.callback = callback
                self.add_item(button)
            source = next((post for post in store.posts(match.id, active_only=True) if post.channel_id != channel_id and post.jump_url), None)
            if source:
                self.add_item(discord.ui.Button(label="Bestaand bericht", url=source.jump_url, row=row))
        back = discord.ui.Button(label="Mijn reservaties", style=discord.ButtonStyle.secondary, row=0)
        back.callback = self.bookings
        self.add_item(back)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id or interaction.channel_id != self.channel_id:
            await interaction.response.send_message("Open je eigen `/bookings` in de chat waar je wilt delen.", ephemeral=True)
            return False
        return True

    async def bookings(self, interaction):
        if not await self.interaction_check(interaction):
            return
        await interaction.response.defer(thinking=False)
        from .discord_ui import BookingsView
        try:
            view = await BookingsView.load(self.owner_id)
            await interaction.edit_original_response(content=None, embed=view.embed(), view=view)
            self.stop()
        except Exception as error:
            logger.warning("Bookings panel reload failed: {}", type(error).__name__)
            await interaction.edit_original_response(content="Het overzicht laden lukt nog niet. Probeer `/bookings` opnieuw.")


async def transfer_share(interaction, owner_id, booking_id, *, move=False, tag_everyone=False, panel=None):
    if (interaction.user.id != owner_id or (panel and interaction.channel_id != panel.channel_id)
            or getattr(getattr(interaction, "context", None), "dm_channel", False)):
        await interaction.response.send_message("Open je eigen `/bookings` in de gewenste groepschat of het serverkanaal.", ephemeral=True)
        return
    if tag_everyone and not getattr(interaction.app_permissions, "mention_everyone", False):
        await interaction.response.send_message("De app mag hier geen @everyone taggen.", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())
        return
    await interaction.response.defer(thinking=False)
    bot, post, send_attempted = interaction.client, None, False
    async with bot._match_publish_lock, bot._booking_lock, bot._match_action_lock:
        try:
            await asyncio.to_thread(shareable_booking, booking_id)
            match = bot.match_store.for_booking(owner_id, booking_id)
            if match is None:
                raise MatchError("Open `/bookings` opnieuw om deze reservatie te delen.")
            post, created = bot.match_store.reserve_post(match.id, owner_id, interaction.channel_id, interaction.guild_id, move=move)
            if created:
                send_attempted = True
                post = await send_post(interaction, match, post, tag_everyone=tag_everyone)
            elif post.message_id is None:
                raise MatchError("Delen nog niet bevestigd. Controleer deze chat en klik op Vernieuwen op het groepsbericht; de oude berichten blijven behouden.")
            if move:
                retired = bot.match_store.move_posts(match.id, owner_id, post.id)
                pending = await cleanup_posts(interaction, match, retired)
                status = f"Verplaatst naar deze chat: {post.jump_url}. De deelnemerslijst is behouden."
                if pending:
                    links = " · ".join(old.jump_url or "Oud bericht nog niet bevestigd" for old in pending)
                    status += f"\nOude berichten verwijderen nog niet bevestigd: {links}. Ze zijn gesloten voor aanmelden. Klik daar op Vernieuwen → Verwijder dit groepsbericht."
                elif retired:
                    status += " De oude groepsberichten zijn verwijderd."
            else:
                status = f"{'Ook hier gedeeld' if created else 'Al gedeeld in deze chat'}: {post.jump_url}. Alle chats gebruiken dezelfde deelnemerslijst (maximaal vier spelers)."
        except MatchError as error:
            status = str(error)
        except discord.HTTPException as error:
            if send_attempted and error.status in (400, 403, 404):
                bot.match_store.drop_pending_post(post.id)
            logger.warning("Cross-chat share unconfirmed: {}", type(error).__name__)
            status = "Delen niet bevestigd. Controleer deze chat; de oude berichten blijven behouden."
        except Exception as error:
            logger.warning("Cross-chat share unconfirmed: {}", type(error).__name__)
            status = "Delen niet bevestigd. Controleer deze chat en klik op Vernieuwen op het nieuwe bericht."
        try:
            kwargs = dict(content=status[:2000], allowed_mentions=discord.AllowedMentions.none())
            if panel is not None:
                kwargs["view"] = panel
            await interaction.edit_original_response(**kwargs)
        except Exception as error:
            logger.warning("Cross-chat summary update failed: {}", type(error).__name__)


async def publish_bookings(interaction, owner_id: int, booking_ids, *, tag_everyone=False):
    if interaction.user.id != owner_id:
        await interaction.response.send_message("Alleen de organisator kan de reservatie delen.", ephemeral=True)
        return
    if getattr(getattr(interaction, "context", None), "dm_channel", False):
        await interaction.response.send_message("Open `/bookings` in de gewenste groepschat of het serverkanaal, selecteer je reservaties en klik daar op Deel met groep.", ephemeral=True)
        return
    booking_ids = tuple(dict.fromkeys(booking_ids))
    if not booking_ids or len(booking_ids) > MAX_SHARE_BOOKINGS:
        await interaction.response.send_message(f"Selecteer één tot {MAX_SHARE_BOOKINGS} reservaties om te delen.", ephemeral=True)
        return
    if tag_everyone and not getattr(interaction.app_permissions, "mention_everyone", False):
        await interaction.response.send_message("De app mag hier geen @everyone taggen. Gebruik Deel met groep of geef de app die toestemming in dit kanaal.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        return
    # A component update defer allows a separate public followup, even when
    # the original booking panel is ephemeral. A thinking defer would not.
    await interaction.response.defer(thinking=False)
    bot = interaction.client
    async with bot._match_publish_lock, bot._booking_lock:
        statuses = []
        choices = []
        ping_pending = tag_everyone
        for index, booking_id in enumerate(booking_ids, 1):
            ping_used, status = await _publish_one(interaction, owner_id, booking_id, tag_everyone=ping_pending)
            statuses.append(f"**{index}.** {status}")
            if ping_used:
                ping_pending = False
            match = bot.match_store.for_booking(owner_id, booking_id)
            if match and match.status == "open" and not match.started:
                current = bot.match_store.post_in_channel(match.id, interaction.channel_id)
                if ((current is None or current.message_id is not None)
                        and any(post.channel_id != interaction.channel_id for post in bot.match_store.posts(match.id, active_only=True))):
                    choices.append(booking_id)
        # Updating the private source panel leaves all five public followup
        # slots available for user installs; no extra summary message is sent.
        try:
            kwargs = dict(content="\n".join(statuses)[:2000], allowed_mentions=discord.AllowedMentions.none())
            if choices:
                kwargs["view"] = CrossShareView(bot.match_store, owner_id, interaction.channel_id, choices, tag_everyone=tag_everyone)
            await interaction.edit_original_response(**kwargs)
            return kwargs.get("view")
        except Exception as error:
            logger.warning("Share summary update failed: {}", type(error).__name__)


async def publish_booking(interaction, owner_id: int, booking_id: int, *, tag_everyone=False):
    return await publish_bookings(interaction, owner_id, (booking_id,), tag_everyone=tag_everyone)


class ShareBookingView(discord.ui.View):
    def __init__(self, owner_id, booking_id):
        super().__init__(timeout=900)
        self.owner_id, self.booking_id = owner_id, booking_id
        button = discord.ui.Button(label="Deel met groep", style=discord.ButtonStyle.primary)
        button.callback = self.share
        self.add_item(button)
        tagged = discord.ui.Button(label="Deel + @everyone", style=discord.ButtonStyle.secondary)
        tagged.callback = self.share_everyone
        self.add_item(tagged)

    async def share(self, interaction):
        if await publish_booking(interaction, self.owner_id, self.booking_id) is not None:
            self.stop()

    async def share_everyone(self, interaction):
        if await publish_booking(interaction, self.owner_id, self.booking_id, tag_everyone=True) is not None:
            self.stop()


async def cancel_shared_booking(interaction, owner_id, booking_id):
    bot = interaction.client
    store = getattr(bot, "match_store", None)
    if store is None:
        return
    async with bot._match_action_lock:
        match = store.cancel_booking(owner_id, booking_id)
        if match is not None:
            await sync_other_posts(interaction, match)
