"""Private watcher management, public sharing and notification routing."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from loguru import logger

from .availability_watch import fetch_watch_day, filter_slots, local_now, snapshot_slots
from .config import settings
from . import weather


class WatchDeliveryUnavailable(Exception):
    """The originating chat cannot currently receive this notification."""


@dataclass
class WatchChatSession:
    interaction: object = field(repr=False)
    remaining: int | None = None


def remember_watch_interaction(bot, store, watch, interaction, *, used_followups=0):
    """Keep temporary chat access in memory, without persisting Discord tokens."""
    channel_id = getattr(interaction, "channel_id", None)
    if channel_id is None:
        return
    owner = interaction.user.id == watch.owner_id == settings.discord_owner_id
    if watch.origin_channel_id is None and owner:
        store.bind_origin(watch.id, watch.owner_id, channel_id, interaction.guild_id)
        watch = store.get(watch.id)
    if channel_id != watch.origin_channel_id:
        return
    if not hasattr(bot, "_watch_interactions"):
        bot._watch_interactions = {}
    sessions = bot._watch_interactions.setdefault(watch.id, {})
    limited = not interaction.is_guild_integration()
    # One slash command may reopen several watches. They share Discord's
    # followup quota for that interaction rather than each receiving five sends.
    session = next((item for destinations in bot._watch_interactions.values()
        for item in destinations.values() if item.interaction is interaction), None)
    if session is None:
        session = WatchChatSession(interaction, 5 - used_followups if limited else None)
    elif session.remaining is not None:
        session.remaining = min(session.remaining, 5 - used_followups)
    if owner:
        sessions["private"] = session
    if (watch.visibility == "public" and interaction.channel_id == watch.public_channel_id
            and getattr(getattr(interaction, "app_permissions", None), "mention_everyone", False)):
        sessions["public"] = session


def chat_session(bot, watch, mode):
    sessions = getattr(bot, "_watch_interactions", {}).get(watch.id, {})
    session = sessions.get(mode)
    if session is not None:
        interaction = session.interaction
        if (interaction.channel_id == watch.origin_channel_id
                and not interaction.is_expired()
                and (mode == "public" or interaction.user.id == watch.owner_id)):
            if session.remaining is not None and session.remaining <= 0:
                raise WatchDeliveryUnavailable("Discords berichtlimiet voor deze chatactie is bereikt. Vernieuw de watcher in de oorspronkelijke chat.")
            return session
        sessions.pop(mode, None)
    raise WatchDeliveryUnavailable("Chattoegang verlopen of na herstart nog niet vernieuwd. Klik in de oorspronkelijke chat op Status vernieuwen of Vernieuw groepsbericht; privéchattoegang geldt 15 minuten.")


def time_line(watch, stamp, courts, names, forecast):
    start = datetime.combine(watch.day, datetime.strptime(stamp, "%H:%M").time(), ZoneInfo(settings.padel_local_tz))
    badge = forecast.badge(start) if forecast else ""
    court_names = ", ".join(names.get(cid, f"Veld {cid}") for cid in courts)
    return f"**{stamp}**{' · ' + badge if badge else ''} · {court_names}"


def watch_embed(watch, snapshot=None, names=None, forecast=None, *, public_post=False):
    snapshot = snapshot if snapshot is not None else watch.snapshot
    names = names or {}
    running = watch.active and watch.ends_at > local_now()
    body = f"**{watch.day:%d/%m/%Y} · {watch.from_hour:02}:00–{watch.to_hour:02}:00**\n"
    badge = forecast.day_badge(watch.day) if forecast else ""
    if badge:
        body += f"Leuven {badge}\n"
    destination = "Openbaar · zichtbaar voor iedereen in deze chat" if watch.visibility == "public" else "Privé · alleen voor jou in deze chat"
    body += f"{'Actief' if running else 'Gestopt / afgelopen'} · {destination} · controle elke {max(30, settings.padelbot_watcher_poll_s) / 60:g} min.\n"
    if watch.chat_url:
        body += f"Gekoppelde chat: {watch.chat_url}\n"
    if watch.visibility == "private" or not watch.public_live:
        body += "Chatmeldingen werken tot 15 minuten na een knopactie. Vernieuw hier om ze te hervatten.\n"
    body += "Starttijden binnen dit venster, inclusief het einduur.\n\n"
    if watch.error:
        body += f"⚠️ {watch.error}\n"
    if snapshot is None:
        body += "De eerste controle volgt nog."
    elif not snapshot:
        body += "Geen vrije starttijden in dit venster. Ik meld het zodra er iets vrijkomt." if running else "Geen vrije starttijden."
    else:
        body += "\n".join(time_line(watch, stamp, courts, names, forecast) for stamp, courts in sorted(snapshot.items()))
    if watch.day > local_now().date() + timedelta(days=settings.padel_opens_days_ahead):
        body += "\nKUL opent slots maximaal 168 uur vooraf; de watcher blijft wachten."
    embed = discord.Embed(title=f"Watcher #{watch.id}", description=body[:4096], color=0x377DFF if running else 0x888888)
    if not public_post:
        body = embed.description + "\n\nKies onderaan **Privé** of **Openbaar + @everyone**. De watcher blijft in de chat waar je hem gestart hebt."
        if watch.public_pending:
            body += "\nDelen is nog niet bevestigd. Controleer de chat voordat je opnieuw deelt."
        embed.description = body[:4096]
    footer = "Alleen de organisator kan de watcher beheren of boeken." if public_post else "Alleen meldingen; boeken vraagt je bevestiging."
    if forecast:
        footer += " · " + weather.CREDIT
    embed.set_footer(text=footer)
    return embed


def everyone_mentions():
    return discord.AllowedMentions(everyone=True, users=False, roles=False, replied_user=False)


async def deliver_notification(bot, watch, snapshot, names, opened, closed, forecast):
    embed = notification_embed(watch, snapshot, names, opened, closed, forecast)
    if watch.visibility == "public":
        fingerprint = json.dumps(snapshot, sort_keys=True)
        if watch.public_alert_attempt == fingerprint:
            return "Publieke melding eerder geprobeerd; controleer het groepsbericht. Er wordt niet opnieuw getagd."
        if watch.public_channel_id != watch.origin_channel_id:
            raise WatchDeliveryUnavailable("De publieke watcher is niet aan de oorspronkelijke chat gekoppeld. Open de watcher daar opnieuw.")
        attempted = False
        try:
            channel = None
            session = None
            if watch.public_live:
                channel = bot.get_channel(watch.origin_channel_id) or await bot.fetch_channel(watch.origin_channel_id)
                guild = getattr(channel, "guild", None)
                if guild is None or guild.id != watch.origin_guild_id or guild.me is None:
                    raise WatchDeliveryUnavailable("De oorspronkelijke chat is niet bereikbaar. Controleer de serverinstallatie en kanaalrechten.")
                if not channel.permissions_for(guild.me).mention_everyone:
                    raise WatchDeliveryUnavailable("De app mag in de oorspronkelijke chat geen @everyone taggen. Controleer de kanaalrechten.")
            else:
                session = chat_session(bot, watch, "public")
            # A lost response may still have pinged the group. Persist before
            # sending so a restart cannot repeat an uncertain everyone ping.
            bot.watch_store.mark_public_alert(watch.id, watch.owner_id, fingerprint)
            attempted = True
            kwargs = dict(content="@everyone 🎾 Padel-beschikbaarheid gewijzigd", embed=embed,
                view=PublicWatchView(bot.watch_store, watch.id), allowed_mentions=everyone_mentions())
            if channel is not None:
                await channel.send(**kwargs)
            else:
                if session.remaining is not None:
                    session.remaining -= 1
                message = await session.interaction.followup.send(**kwargs, ephemeral=False, wait=True)
                if message.flags.ephemeral:
                    bot.watch_store.mark_public_alert(watch.id, watch.owner_id, None)
                    raise WatchDeliveryUnavailable("Discord staat in deze chat alleen privé-antwoorden toe. Controleer de toestemming voor openbare appberichten.")
        except WatchDeliveryUnavailable:
            raise
        except Exception as error:
            if attempted and isinstance(error, discord.HTTPException) and error.status in (400, 403, 404):
                bot.watch_store.mark_public_alert(watch.id, watch.owner_id, None)
            logger.warning("Public watcher notification failed: {}", type(error).__name__)
            if not attempted or isinstance(error, discord.HTTPException) and error.status in (400, 403, 404):
                raise WatchDeliveryUnavailable("Melding in de oorspronkelijke chat geweigerd. Controleer de kanaalrechten of vernieuw daar de watcher.") from error
            return "Publieke melding niet bevestigd; controleer de oorspronkelijke chat. Er wordt niet automatisch opnieuw getagd."
        else:
            # The original shared overview remains useful without pinging again.
            if watch.public_message_id:
                try:
                    kwargs = dict(embed=watch_embed(watch, snapshot, names, forecast, public_post=True),
                        view=PublicWatchView(bot.watch_store, watch.id), allowed_mentions=discord.AllowedMentions.none())
                    if channel is not None:
                        await channel.get_partial_message(watch.public_message_id).edit(**kwargs)
                    else:
                        await session.interaction.followup.edit_message(watch.public_message_id, **kwargs)
                except Exception as error:
                    logger.warning("Shared watcher overview update failed: {}", type(error).__name__)
            return None
    session = chat_session(bot, watch, "private")
    if session.remaining is not None:
        session.remaining -= 1
    await session.interaction.followup.send(embed=embed, view=WatchControlsView(bot.watch_store, watch.id),
        ephemeral=True, wait=True, allowed_mentions=discord.AllowedMentions.none())
    return None


async def publish_watch(interaction, store, watch_id):
    """One explicit public choice authorizes one post and its everyone ping."""
    watch = store.get(watch_id)
    if watch is None or interaction.user.id != watch.owner_id or interaction.user.id != settings.discord_owner_id:
        await interaction.response.send_message("Alleen de eigenaar kan deze watcher delen.", ephemeral=True)
        return
    if watch.origin_channel_id is not None and interaction.channel_id != watch.origin_channel_id:
        await interaction.response.send_message(f"Deze watcher hoort bij de chat waar hij gestart is. Open hem daar: {watch.chat_url}", ephemeral=True)
        return
    if getattr(getattr(interaction, "context", None), "dm_channel", False):
        await interaction.response.send_message("Open `/watcher` in de gewenste groepschat of het serverkanaal en deel daar je watcher.", ephemeral=True)
        return
    if not getattr(getattr(interaction, "app_permissions", None), "mention_everyone", False):
        await interaction.response.send_message("De app mag hier geen @everyone taggen. Geef de app die kanaaltoestemming om openbaar te delen.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        return
    await interaction.response.defer(thinking=False)
    bot = interaction.client
    async with bot._availability_watch_lock:
        watch = store.get(watch_id)
        remember_watch_interaction(bot, store, watch, interaction, used_followups=1)
        if watch.visibility == "public":
            await interaction.followup.send(f"Deze watcher is al openbaar gedeeld: {watch.jump_url}. Kies eerst Privé als je hem opnieuw wilt delen.", ephemeral=True)
            return
        if watch.public_pending:
            await interaction.followup.send("De eerdere deelactie is niet bevestigd. Controleer de chat. Kies Privé om die deelactie terug te zetten voordat je opnieuw deelt.", ephemeral=True)
            return
        if not watch.active or watch.ends_at <= local_now():
            await interaction.followup.send("Deze watcher is gestopt of afgelopen. Start een nieuwe watcher om te delen.", ephemeral=True)
            return
        try:
            watch, slots, names, forecast = await load_panel(store, watch_id)
        except Exception:
            await interaction.followup.send("Beschikbaarheid ophalen mislukt; er is niets openbaar gedeeld. Probeer opnieuw.", ephemeral=True)
            return
        if not watch.active or watch.ends_at <= local_now() or not store.begin_publish(watch_id, watch.owner_id):
            await interaction.followup.send("Deze watcher kan nu niet gedeeld worden. Vernieuw het overzicht.", ephemeral=True)
            return
        guild = bot.get_guild(interaction.guild_id) if interaction.guild_id else None
        live = bool(guild is not None and guild.me is not None)
        preview = replace(watch, visibility="public", public_live=live)
        send_attempted = False
        try:
            send_attempted = True
            message = await interaction.followup.send(content="@everyone 🎾 Padel-watcher gestart",
                embed=watch_embed(preview, snapshot_slots(watch, slots), names, forecast, public_post=True),
                view=PublicWatchView(store, watch_id), ephemeral=False, wait=True, allowed_mentions=everyone_mentions())
            if message.flags.ephemeral:
                store.publication_failed(watch_id, watch.owner_id)
                await message.edit(content="Discord staat hier alleen privé-antwoorden toe. De watcher blijft privé.", embed=None, view=None,
                    allowed_mentions=discord.AllowedMentions.none())
                return
            if not store.finish_publish(watch_id, watch.owner_id, channel_id=interaction.channel_id,
                message_id=message.id, guild_id=interaction.guild_id, live=live):
                raise RuntimeError("Public watcher binding failed")
        except Exception as error:
            if not send_attempted or isinstance(error, discord.HTTPException) and error.status in (400, 403, 404):
                store.publication_failed(watch_id, watch.owner_id)
            logger.warning("Watcher sharing not confirmed: {}", type(error).__name__)
            await interaction.followup.send("Delen niet bevestigd; controleer de chat voordat je opnieuw deelt. Er wordt niet automatisch opnieuw getagd.", ephemeral=True)
            return
        bot.add_view(PublicWatchView(store, watch_id), message_id=message.id)
        watch = store.get(watch_id)
        remember_watch_interaction(bot, store, watch, interaction, used_followups=1)
        status = "✅ Openbaar gedeeld met @everyone. Nieuwe meldingen verschijnen in deze chat met @everyone."
        if not live:
            status += " Vernieuw het groepsbericht om de chattoegang van 15 minuten te verlengen."
        await interaction.edit_original_response(content=status,
            embed=watch_embed(watch, snapshot_slots(watch, slots), names, forecast), view=WatchControlsView(store, watch_id),
            allowed_mentions=discord.AllowedMentions.none())


async def edit_public_overview(interaction, watch, **kwargs):
    if not watch.public_message_id:
        return True
    try:
        if interaction.channel_id == watch.public_channel_id:
            await interaction.followup.edit_message(watch.public_message_id, **kwargs)
        elif watch.public_guild_id:
            bot = interaction.client
            channel = bot.get_channel(watch.public_channel_id) or await bot.fetch_channel(watch.public_channel_id)
            await channel.get_partial_message(watch.public_message_id).edit(**kwargs)
        else:
            return False
        return True
    except Exception as error:
        logger.warning("Shared watcher overview edit failed: {}", type(error).__name__)
        return False


async def redact_public_overview(interaction, watch):
    return await edit_public_overview(interaction, watch,
        content="Deze watcher is nu privé. Nieuwe meldingen zijn alleen voor de organisator zichtbaar in deze chat.",
        embed=None, view=None, allowed_mentions=discord.AllowedMentions.none())


class PublicWatchView(discord.ui.View):
    def __init__(self, store, watch_id):
        super().__init__(timeout=None)
        self.store, self.watch_id = store, watch_id
        refresh = discord.ui.Button(label="Vernieuw groepsbericht", custom_id=f"padel:watch:{watch_id}:publicrefresh")
        refresh.callback = self.refresh
        self.add_item(refresh)
        manage = discord.ui.Button(label="Beheren (organisator)", custom_id=f"padel:watch:{watch_id}:manage")
        async def manage_callback(interaction):
            await WatchControlsView(store, watch_id).act(interaction, "refresh")
        manage.callback = manage_callback
        self.add_item(manage)

    async def refresh(self, interaction):
        watch = self.store.get(self.watch_id)
        message = interaction.message
        # Notifications in the published channel may also offer this read-only
        # refresh. Author/channel checks prevent exporting a private watcher.
        if (watch is None or message is None or message.author.id != interaction.client.user.id
                or interaction.channel_id != watch.public_channel_id):
            await interaction.response.send_message("Dit is niet de gedeelde chat van deze watcher.", ephemeral=True)
            return
        await interaction.response.defer(thinking=False)
        async with interaction.client._availability_watch_lock:
            watch = self.store.get(self.watch_id)
            if watch.visibility != "public":
                await interaction.edit_original_response(content="Deze watcher is nu privé.", embed=None, view=None,
                    allowed_mentions=discord.AllowedMentions.none())
                return
            remember_watch_interaction(interaction.client, self.store, watch, interaction)
            try:
                watch, slots, names, forecast = await load_panel(self.store, self.watch_id)
                snapshot = snapshot_slots(watch, slots)
            except Exception:
                snapshot, names, forecast = watch.snapshot, {}, weather.peek_forecast()
            await interaction.edit_original_response(
                embed=watch_embed(watch, snapshot, names, forecast, public_post=True),
                view=PublicWatchView(self.store, self.watch_id), allowed_mentions=discord.AllowedMentions.none())


def notification_embed(watch, snapshot, names, opened, closed, forecast):
    embed = discord.Embed(title=f"Padel · beschikbaarheid gewijzigd · {watch.day:%d/%m}",
        description=f"Watcher #{watch.id} · {watch.from_hour:02}:00–{watch.to_hour:02}:00", color=0x377DFF)
    if opened:
        lines = [time_line(watch, stamp, snapshot[stamp], names, forecast) for stamp in opened]
        embed.add_field(name="🟢 Vrijgekomen", value="\n".join(lines)[:1024], inline=False)
    if closed:
        lines = []
        for stamp in closed:
            start = datetime.combine(watch.day, datetime.strptime(stamp, "%H:%M").time(), ZoneInfo(settings.padel_local_tz))
            badge = forecast.badge(start) if forecast else ""
            lines.append(f"**{stamp}**{' · ' + badge if badge else ''} · geen vrij veld meer")
        embed.add_field(name="🔴 Volzet", value="\n".join(lines)[:1024], inline=False)
    embed.set_footer(text="Beschikbaarheid kan veranderen. Boeken vraagt je bevestiging." + (" · " + weather.CREDIT if forecast else ""))
    return embed


async def load_panel(store, watch_id):
    watch = store.get(watch_id)
    slots, names = await asyncio.to_thread(fetch_watch_day, watch.day)
    forecast = await asyncio.to_thread(weather.get_forecast)
    return watch, slots, names, forecast


class WatchControlsView(discord.ui.View):
    def __init__(self, store, watch_id):
        super().__init__(timeout=None)
        self.store, self.watch_id = store, watch_id
        watch = store.get(watch_id)
        closed = watch is None or not watch.active or watch.ends_at <= local_now()
        for action, label, style, disabled in [
            ("slots", "Bekijk vrije slots / boek", discord.ButtonStyle.primary, False),
            ("refresh", "Status vernieuwen", discord.ButtonStyle.secondary, False),
            ("stop", "Stop watcher", discord.ButtonStyle.danger, closed),
        ]:
            button = discord.ui.Button(label=label, style=style, custom_id=f"padel:watch:{watch_id}:{action}", disabled=disabled)
            async def callback(interaction, action=action):
                await self.act(interaction, action)
            button.callback = callback
            self.add_item(button)
        for action, label, style, disabled in [
            ("private", "Privé", discord.ButtonStyle.secondary, False),
            ("public", "Openbaar + @everyone", discord.ButtonStyle.primary, closed),
        ]:
            button = discord.ui.Button(label=label, style=style, row=1, custom_id=f"padel:watch:{watch_id}:{action}", disabled=disabled)
            async def callback(interaction, action=action):
                await self.act(interaction, action)
            button.callback = callback
            self.add_item(button)

    async def interaction_check(self, interaction):
        watch = self.store.get(self.watch_id)
        if (watch is None or not settings.discord_owner_id or interaction.user.id != settings.discord_owner_id
                or interaction.user.id != watch.owner_id):
            await interaction.response.send_message("Alleen de eigenaar kan deze watcher bedienen.", ephemeral=True)
            return False
        if watch.origin_channel_id is not None and getattr(interaction, "channel_id", None) != watch.origin_channel_id:
            await interaction.response.send_message(f"Deze watcher hoort bij de chat waar hij gestart is. Open hem daar: {watch.chat_url}", ephemeral=True)
            return False
        return True

    async def act(self, interaction, action):
        if not await self.interaction_check(interaction):
            return
        if action == "public":
            await publish_watch(interaction, self.store, self.watch_id)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        watch = self.store.get(self.watch_id)
        remember_watch_interaction(interaction.client, self.store, watch, interaction)
        if action == "private":
            async with interaction.client._availability_watch_lock:
                self.store.make_private(self.watch_id, watch.owner_id)
                redacted = await redact_public_overview(interaction, watch)
            watch = self.store.get(self.watch_id)
            content = "🔒 Watcher privé. Nieuwe meldingen zijn alleen voor jou zichtbaar in deze chat. Eerder verstuurde meldingen blijven zichtbaar."
            if not redacted:
                content += " Het oude gedeelde overzicht kan in die chat met Vernieuwen worden afgeschermd."
            await interaction.edit_original_response(content=content, embed=watch_embed(watch, forecast=weather.peek_forecast()),
                view=WatchControlsView(self.store, self.watch_id))
            return
        if action == "stop":
            async with interaction.client._availability_watch_lock:
                self.store.stop(self.watch_id, watch.owner_id)
                watch = self.store.get(self.watch_id)
                if watch.visibility == "public":
                    await edit_public_overview(interaction, watch,
                        embed=watch_embed(watch, forecast=weather.peek_forecast(), public_post=True),
                        view=PublicWatchView(self.store, self.watch_id), allowed_mentions=discord.AllowedMentions.none())
            watch = self.store.get(self.watch_id)
            await interaction.edit_original_response(content="✅ Watcher gestopt.",
                embed=watch_embed(watch, forecast=weather.peek_forecast()), view=WatchControlsView(self.store, self.watch_id))
            return
        try:
            watch, slots, names, forecast = await load_panel(self.store, self.watch_id)
        except Exception:
            await interaction.edit_original_response(content="KUL ophalen mislukt. Probeer opnieuw of bekijk de laatst gekende status hieronder.",
                embed=watch_embed(self.store.get(self.watch_id), forecast=weather.peek_forecast()),
                view=WatchControlsView(self.store, self.watch_id))
            return
        if action == "slots":
            from .discord_ui import DaySlotsView
            target = datetime.combine(watch.day, datetime.min.time(), ZoneInfo(settings.padel_local_tz))
            view = DaySlotsView(watch.owner_id, target, filter_slots(watch, slots), names, forecast=forecast)
            status = "De watcher blijft actief." if watch.active and watch.ends_at > local_now() else "De watcher is gestopt / afgelopen."
            await interaction.edit_original_response(content=f"Vrije starttijden voor {watch.label()}. {status}",
                embed=view.embed(), view=view)
        else:
            await interaction.edit_original_response(content=None,
                embed=watch_embed(watch, snapshot_slots(watch, slots), names, forecast),
                view=WatchControlsView(self.store, self.watch_id))


class WatchListView(discord.ui.View):
    def __init__(self, store, owner_id):
        super().__init__(timeout=None)
        self.store, self.owner_id = store, owner_id
        store.expire()
        self.watches = store.list_active(owner_id)
        if self.watches:
            select = discord.ui.Select(custom_id="padel:watch:list", placeholder="Kies een watcher om te bekijken of stoppen",
                options=[discord.SelectOption(label=f"#{watch.id} · {watch.label()}", value=str(watch.id)) for watch in self.watches])
            async def choose(interaction):
                await WatchControlsView(store, int(select.values[0])).act(interaction, "refresh")
            select.callback = choose
            self.add_item(select)
        button = discord.ui.Button(label="Vernieuwen", custom_id="padel:watch:listrefresh")
        async def refresh(interaction):
            view = WatchListView(store, owner_id)
            await interaction.response.edit_message(embed=view.embed(), view=view)
        button.callback = refresh
        self.add_item(button)

    async def interaction_check(self, interaction):
        if not self.owner_id or interaction.user.id != self.owner_id or self.owner_id != settings.discord_owner_id:
            await interaction.response.send_message("Alleen de eigenaar kan watchers beheren.", ephemeral=True)
            return False
        return True

    def embed(self):
        forecast = weather.peek_forecast()
        lines = []
        for watch in self.watches:
            badge = forecast.day_badge(watch.day) if forecast else ""
            mode = "Openbaar" if watch.visibility == "public" else "Privé"
            chat = f" · [Chat]({watch.chat_url})" if watch.chat_url else " · kies een chat om te koppelen"
            lines.append(f"**#{watch.id} · {watch.label()}** · {mode}{' · ' + badge if badge else ''}{chat}")
        body = "\n".join(lines) or "Geen actieve watchers."
        body += "\n\nNieuw: `/watcher dag:0 van:13 tot:20` · 0 = vandaag, 1 = morgen.\nKies na het instellen Privé of Openbaar + @everyone."
        return discord.Embed(title=f"Mijn watchers ({len(self.watches)})", description=body[:4096], color=0x377DFF)
