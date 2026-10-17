"""Discord panels for availability, bookings and weekly automation."""
from __future__ import annotations
import asyncio
import os
import sys
import time as _time
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from loguru import logger

from .auth import renew_access_token
from .booking import BookingResult
from .booking_manager import list_my_bookings
from .client import BackboneClient
from .config import settings
from .live import SlotsView, render_bookings_embed, render_slots_embed
from .payment import payment_message
from .rules_store import RulesStore
from .matches import MatchStore
from .scheduler import FireSpec, run_scheduler, _fire_one_rule
from .store import TokenStore
from .watcher import WatchHit, run_watcher
from .availability_watch import WatchStore, run_availability_watcher


def _is_owner(interaction: discord.Interaction) -> bool:
    return settings.discord_owner_id != 0 and interaction.user.id == settings.discord_owner_id


async def _deny_if_not_owner(interaction: discord.Interaction) -> bool:
    if not _is_owner(interaction):
        await interaction.response.send_message(
            ":no_entry_sign: Not authorized.", ephemeral=True
        )
        return True
    return False


class PadelBot(commands.Bot):
    def __init__(self, store: RulesStore):
        intents = discord.Intents.default()
        super().__init__(
            command_prefix="!padelbot ", intents=intents,
            allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
            allowed_contexts=app_commands.AppCommandContext(
                guild=True, dm_channel=True, private_channel=True,
            ),
        )
        self.rules_store = store
        self.match_store = MatchStore(store.path)
        self.watch_store = WatchStore(store.path)
        self._availability_watch_lock = asyncio.Lock()
        self._watch_interactions = {}
        self._match_publish_lock = asyncio.Lock()
        self._match_action_lock = asyncio.Lock()
        self._booking_lock = asyncio.Lock()
        self._scheduler_task: asyncio.Task | None = None
        self._watcher_task: asyncio.Task | None = None
        self._availability_watcher_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._slots_refresh_task: asyncio.Task | None = None
        self._bookings_refresh_task: asyncio.Task | None = None
        self._reminder_task: asyncio.Task | None = None
        self._notice_cleanup_task: asyncio.Task | None = None
        self._hub_posts_task: asyncio.Task | None = None
        self._last_keepalive_ok: bool = True
        self._last_keepalive_at: float = 0.0
        self.platform=None
        self._delivery_task=None
        self._identity_task=None
        if settings.padelbot_platform_root is not None:
            from .web_store import PlatformStore
            from .platform_service import PlatformService
            self.platform=PlatformService(PlatformStore(settings.padelbot_platform_root,settings.padelbot_platform_owner))

    async def setup_hook(self) -> None:
        logger.info("setup_hook: syncing slash commands...")
        try:
            # User-installed commands in group DMs must be registered globally.
            cmds = await self.tree.sync()
            logger.info("Synced {} global slash commands for server, DM and user installs.", len(cmds))
            if settings.discord_guild_id:
                guild = discord.Object(id=settings.discord_guild_id)
                # Guild commands override global commands in this server.
                # Replace the complete list; copy_global_to otherwise merges it.
                self.tree.clear_commands(guild=guild)
                self.tree.copy_global_to(guild=guild)
                cmds = await self.tree.sync(guild=guild)
                logger.info("Synced {} slash commands to guild {}.", len(cmds), settings.discord_guild_id)
        except Exception:
            logger.exception("Failed to sync slash commands")
            raise

        # Register the persistent SlotsView so day-buttons keep working across restarts
        if settings.discord_owner_id and self.platform is None:
            self.add_view(SlotsView(settings.discord_owner_id))
        from .match_ui import post_view
        for match in self.match_store.future_matches():
            for post in self.match_store.posts(match.id):
                if post.state != "removed":
                    self.add_view(post_view(self.match_store, match, post, self._match_action_lock), message_id=post.message_id)
        from .watch_ui import WatchControlsView, WatchListView, PublicWatchView
        self.watch_store.expire()
        if settings.discord_owner_id:
            self.add_view(WatchListView(self.watch_store, settings.discord_owner_id))
        for watch in self.watch_store.list_active(settings.discord_owner_id):
            self.add_view(WatchControlsView(self.watch_store, watch.id))
            if watch.visibility == "public":
                self.add_view(PublicWatchView(self.watch_store, watch.id))

    async def on_ready(self) -> None:
        logger.info("Bot ready as {} (id={}).", self.user, getattr(self.user, "id", "?"))
        from .hub import HubStore
        from .identity import run_identity_refresh
        hub=HubStore(self.match_store)
        hub.sync_guilds(self.guilds)
        hub.backfill_shared_contexts()
        if self._identity_task is None or self._identity_task.done():
            self._identity_task=asyncio.create_task(run_identity_refresh(self,hub))
        owner_ok = bool(settings.discord_owner_id)
        if self.platform is not None and (self._delivery_task is None or self._delivery_task.done()):
            from .discord_delivery import run_deliveries
            self._delivery_task=asyncio.create_task(run_deliveries(self,self.platform))
        if self.platform is not None and (self._reminder_task is None or self._reminder_task.done()):
            from .discord_lifecycle import run_booking_lifecycle
            self._reminder_task=asyncio.create_task(run_booking_lifecycle(self,self.platform))
        guild_ok = bool(settings.discord_guild_id)
        logger.info(
            "Owner gating: {} · Guild scope: {} · Type /checkbookings in Discord to test.",
            "ON" if owner_ok else "NO OWNER CONFIGURED — all commands are denied",
            f"guild={settings.discord_guild_id}" if guild_ok else "global",
        )

        from .match_notices import run_notice_cleanup
        if self._notice_cleanup_task is None or self._notice_cleanup_task.done():
            self._notice_cleanup_task = asyncio.create_task(run_notice_cleanup(self))
        if self._hub_posts_task is None or self._hub_posts_task.done():
            from .hub_posts import run_hub_post_updates
            self._hub_posts_task=asyncio.create_task(run_hub_post_updates(self))

        # on_ready can fire multiple times on reconnect — only start tasks once.
        if self._scheduler_task is not None and not self._scheduler_task.done():
            return

        # Start the precise scheduler (Type A: weekly opening moment)
        if self.platform is None:
            self._scheduler_task = asyncio.create_task(
                run_scheduler(self.rules_store, on_attempt=self._announce_attempt, booking_lock=self._booking_lock))
        else:
            # The web worker schedules every profile, including the existing owner.
            self._scheduler_task=asyncio.create_task(self._platform_lifetime())
        # Start the keepalive task — periodic silent refresh to keep the
        # Shibboleth session warm and avoid idle-timeout MFA prompts.
        if self.platform is None:
            self._keepalive_task = asyncio.create_task(self._run_keepalive())
            self._slots_refresh_task = asyncio.create_task(self._run_slots_refresh())
            self._bookings_refresh_task = asyncio.create_task(self._run_bookings_refresh())
            self._reminder_task = asyncio.create_task(self._run_session_reminders())
        # Platform profiles renew on their queued work. Legacy owner polling must
        # not boot Android every minute while nobody is using the new hub.

        # Start the slot watcher (Type B: catches cancellations within the
        # 7-day window, auto-books if a watched slot frees up).
        self._watcher_task = asyncio.create_task(
            run_watcher(
                self.rules_store,
                on_hit=self._on_watch_hit,
                poll_interval_s=settings.padelbot_watcher_poll_s,
            )
        ) if self.platform is None else None
        self._availability_watcher_task = asyncio.create_task(run_availability_watcher(self))

    async def close(self):
        if self._identity_task is not None:
            self._identity_task.cancel();await asyncio.gather(self._identity_task,return_exceptions=True)
        if self._reminder_task is not None:
            self._reminder_task.cancel();await asyncio.gather(self._reminder_task,return_exceptions=True)
        if self._delivery_task is not None:
            self._delivery_task.cancel();await asyncio.gather(self._delivery_task,return_exceptions=True)
        if self._hub_posts_task is not None:
            self._hub_posts_task.cancel()
            await asyncio.gather(self._hub_posts_task,return_exceptions=True)
        if self._notice_cleanup_task is not None:
            self._notice_cleanup_task.cancel()
            try:
                await self._notice_cleanup_task
            except asyncio.CancelledError:
                pass
        await super().close()

    async def _platform_lifetime(self):
        await asyncio.Event().wait()

    async def on_guild_join(self,guild):
        from .hub import HubStore
        HubStore(self.match_store).remember_guild(guild.id,guild.name)

    async def on_guild_remove(self,guild):
        from .hub import HubStore
        HubStore(self.match_store).remember_guild(guild.id,guild.name,active=False)

    def _has_component_handler(self, interaction: discord.Interaction) -> bool:
        # discord.py dispatches the view before on_interaction. Consult the same
        # registry so a slow live callback is never mistaken for an expired one.
        # If the SDK changes this private registry, leave live dispatch alone.
        views = getattr(self._connection._view_store, "_views", None)
        if not isinstance(views, dict):
            return True
        key = (interaction.data.get("component_type"), interaction.data.get("custom_id"))
        message = interaction.message
        ids = [message.id]
        # Older discord.py versions also indexed initial responses by interaction.
        for attr in ("interaction_metadata", "interaction"):
            metadata = getattr(message, attr, None)
            if metadata is not None:
                ids.append(metadata.id)
        ids.append(None)
        for message_id in ids:
            item = views.get(message_id, {}).get(key)
            if item is not None and item.view is not None and not item.view.is_finished():
                return True
        return False

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Recover expired reservation panels without replaying their action."""
        try:
            from .hub import HubStore
            from .identity import remember_interaction
            remember_interaction(HubStore(self.match_store),interaction)
        except Exception as error:
            logger.warning('Discord identity capture deferred: {}',type(error).__name__)
        if interaction.type != discord.InteractionType.component:
            return
        message = interaction.message
        if message is None or self.user is None or message.author.id != self.user.id:
            return
        if interaction.response.is_done() or self._has_component_handler(interaction):
            return
        custom_id = interaction.data.get("custom_id", "")
        if custom_id.startswith("padel:watch:"):
            from .watch_ui import WatchControlsView, WatchListView, PublicWatchView
            parts = custom_id.split(":")
            if len(parts) == 4 and parts[2].isdigit() and parts[3] in ("slots", "refresh", "stop", "private", "public", "manage", "publicrefresh"):
                watch_id, action = int(parts[2]), parts[3]
                if action == "publicrefresh":
                    await PublicWatchView(self.watch_store, watch_id).refresh(interaction)
                else:
                    await WatchControlsView(self.watch_store, watch_id).act(interaction, "refresh" if action == "manage" else action)
            elif custom_id in ("padel:watch:list", "padel:watch:listrefresh"):
                if await _deny_if_not_owner(interaction):
                    return
                await interaction.response.defer(thinking=True, ephemeral=True)
                view = WatchListView(self.watch_store, settings.discord_owner_id)
                await interaction.edit_original_response(embed=view.embed(), view=view)
            return
        if not any((embed.title or "").startswith("Mijn reservaties (") for embed in message.embeds):
            return
        if await _deny_if_not_owner(interaction):
            return

        if self.platform is not None:
            from .discord_platform import command
            await command(interaction,self.platform,'bookings')
            return

        # Reply immediately; KUL may need to renew its session before reading.
        await interaction.response.defer(ephemeral=True, thinking=True)
        from .discord_ui import BookingsView
        try:
            view = await BookingsView.load(settings.discord_owner_id)
        except Exception as error:
            logger.warning("Expired bookings panel reload failed: {}", type(error).__name__)
            await interaction.edit_original_response(
                content="Dit overzicht was verlopen. Reservaties ophalen mislukt; probeer `/bookings` opnieuw.",
                embed=None, view=None,
            )
            return
        await interaction.edit_original_response(
            content="Het vorige overzicht was verlopen. Kies je reservatie opnieuw in dit bijgewerkte overzicht. Er is nog niets geannuleerd of gedeeld.",
            embed=view.embed(), view=view,
        )

    async def _announce_attempt(
        self,
        spec: FireSpec,
        ok: bool,
        result: BookingResult | None,
        err: str | None,
    ) -> None:
        """Called by the scheduler after each booking attempt."""
        owner = await self._owner()
        if owner is None:
            return
        if ok and result:
            paid_line = payment_message(result)
            msg = (
                f":tennis: **Booked!** rule `{spec.rule.label()}` "
                f"→ slot {spec.target_slot_local:%Y-%m-%d %H:%M %Z}\n"
                f"sale_id `{result.sale_id}` booking_id `{result.booking_id}` "
                f"amount `{result.amount} EUR`\n"
                f"{paid_line}"
            )
        else:
            msg = (
                f":x: **Booking FAILED** for rule `{spec.rule.label()}` "
                f"(slot {spec.target_slot_local:%Y-%m-%d %H:%M %Z})\n"
                f"```{(err or 'unknown')[:1500]}```"
            )
        try:
            await owner.send(msg)
        except discord.HTTPException as e:
            logger.warning("Could not DM owner: {}", e)

    async def _run_keepalive(self) -> None:
        """Periodically silent-refresh to keep the Shibboleth session alive.

        Runs every `padelbot_keepalive_interval_s` (default 25 min). On the
        first failure DMs the owner once. Keeps retrying without triggering
        MFA, so transient network failures and a new local login recover.
        """
        import time as _time
        # First refresh after a short delay so we don't hit the API the moment
        # the bot starts (the user may have just done /login).
        await asyncio.sleep(60)
        interval = settings.padelbot_keepalive_interval_s
        failure_notified = False
        while True:
            try:
                def _do_refresh() -> tuple[bool, str | None]:
                    try:
                        renew_access_token(TokenStore(settings.padelbot_key_file, settings.padelbot_token_file))
                        return True, None
                    except Exception as e:
                        return False, f"{type(e).__name__}: {e}"

                ok, err = await asyncio.to_thread(_do_refresh)
                self._last_keepalive_ok = ok
                self._last_keepalive_at = _time.time()
                if ok:
                    failure_notified = False
                    logger.info("Keepalive: silent refresh OK.")
                else:
                    logger.warning("Keepalive: silent refresh FAILED: {}", err)
                    owner = await self._owner() if not failure_notified else None
                    if owner:
                        try:
                            await owner.send(
                                ":warning: **Padel bot — session renewal failed.**\n"
                                f"Renewal failed: `{err}`.\n"
                                "If re-login is required, run `padelbot login --http` or `padelbot login`. "
                                "Keepalive retries automatically; no bot restart is needed."
                            )
                        except discord.HTTPException as e:
                            logger.warning("Could not DM owner about session death: {}", e)
                    failure_notified = True

                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                logger.info("Keepalive stopping.")
                raise
            except Exception:
                logger.exception("Keepalive loop crashed — sleeping {}s before retry", interval)
                await asyncio.sleep(interval)

    async def _on_watch_hit(self, hit: WatchHit) -> bool:
        """A watched slot just became available within the 7-day window
        (most likely someone cancelled). Auto-book and DM the owner."""
        async with self._booking_lock:
            current_rule = self.rules_store.get_rule(hit.rule.id)
            if current_rule is None or not current_rule.enabled:
                return False
            if (current_rule.day_of_week, current_rule.time_local, current_rule.end_time_local) != (
                    hit.rule.day_of_week, hit.rule.time_local, hit.rule.end_time_local):
                return False
            if self.rules_store.has_booking_for_day(hit.rule.id, hit.target_slot_local.date()):
                return True
            spec = FireSpec(current_rule, hit.target_slot_local, datetime.now(ZoneInfo(settings.padel_local_tz)))
            try:
                # Fetch again under the shared lock before creating a reservation.
                ok, res, err = await asyncio.to_thread(_fire_one_rule, spec)
            except Exception as e:
                ok, res, err = False, None, f"{type(e).__name__}: {e}"
            self.rules_store.record(
                rule_id=hit.rule.id,
                success=ok,
                sale_id=(res.sale_id if res else None),
                booking_id=(res.booking_id if res else None),
                target_slot_iso=spec.target_slot_local.isoformat(),
                error=err,
            )
        owner = await self._owner()
        if owner is None:
            return ok
        if ok and res:
            paid_line = "\n" + payment_message(res)
            msg = (
                f":eyes: **Watched slot opened up — booked!**\n"
                f"Rule `{hit.rule.label()}` for slot {spec.target_slot_local:%Y-%m-%d %H:%M %Z}\n"
                f"sale `{res.sale_id}` booking `{res.booking_id}` amount `{res.amount} EUR`"
                f"{paid_line}"
            )
        else:
            msg = (
                f":mag: **Watched slot was available** but booking FAILED for `{hit.rule.label()}`\n"
                f"```{(err or 'unknown')[:1500]}```"
            )
        try:
            await owner.send(msg)
        except discord.HTTPException as e:
            logger.warning("Could not DM owner: {}", e)
        return ok

    # ---- live channel refreshers ----

    async def _fetch_my_state(self):
        """Pulls slots+bookings+names. The sync API work runs in a worker thread."""
        def _sync():
            from .booking import find_slots_window, get_product_names
            from . import weather
            from datetime import datetime as _dt
            with BackboneClient() as c:
                me = c.me()
                slots = find_slots_window(c, start_date=_dt.now(ZoneInfo(settings.padel_local_tz)), days=8)
                mine = list_my_bookings(c, member_id=me["id"], upcoming_only=True)
                court_ids = {s.bookable_product_id for s in slots} | {
                    b.raw.get("productId") for b in mine if b.raw.get("productId")
                }
                names = get_product_names(c, {cid for cid in court_ids if cid}) if court_ids else {}
            weather.get_forecast()
            return me, slots, mine, names
        return await asyncio.to_thread(_sync)

    async def _refresh_slots_message(self) -> None:
        chan_id = self.rules_store.kv_int("slots_channel_id")
        msg_id = self.rules_store.kv_int("slots_message_id")
        if not chan_id or not msg_id:
            return
        try:
            channel = self.get_channel(chan_id) or await self.fetch_channel(chan_id)
            msg = await channel.fetch_message(msg_id)
        except (discord.NotFound, discord.Forbidden) as e:
            logger.warning("Slots message gone: {}", e)
            return
        try:
            me, slots, mine, names = await self._fetch_my_state()
        except Exception:
            logger.exception("slots refresh fetch failed")
            return
        tz = ZoneInfo(settings.padel_local_tz)
        my_days = {b.start_dt.astimezone(tz).strftime("%Y-%m-%d") for b in mine}
        embed = render_slots_embed(
            slots, names,
            member_name=f"{me.get('firstName')} {me.get('lastName')}",
            my_booking_days=my_days,
            my_bookings_count=len(mine),
        )
        await msg.edit(embed=embed, view=SlotsView(settings.discord_owner_id))

    async def _refresh_bookings_message(self) -> None:
        chan_id = self.rules_store.kv_int("bookings_channel_id")
        msg_id = self.rules_store.kv_int("bookings_message_id")
        if not chan_id or not msg_id:
            return
        try:
            channel = self.get_channel(chan_id) or await self.fetch_channel(chan_id)
            msg = await channel.fetch_message(msg_id)
        except (discord.NotFound, discord.Forbidden) as e:
            logger.warning("Bookings message gone: {}", e)
            return
        try:
            me, _slots, mine, names = await self._fetch_my_state()
        except Exception:
            logger.exception("bookings refresh fetch failed")
            return
        embed = render_bookings_embed(mine, names, member_name=f"{me.get('firstName')} {me.get('lastName')}")
        await msg.edit(embed=embed)

    async def _run_slots_refresh(self) -> None:
        await asyncio.sleep(15)  # let the bot settle
        while True:
            try:
                await self._refresh_slots_message()
                await asyncio.sleep(settings.padelbot_slots_refresh_s)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("slots refresh loop crashed")
                await asyncio.sleep(settings.padelbot_slots_refresh_s)

    async def _run_bookings_refresh(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                await self._refresh_bookings_message()
                await asyncio.sleep(settings.padelbot_bookings_refresh_s)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("bookings refresh loop crashed")
                await asyncio.sleep(settings.padelbot_bookings_refresh_s)

    async def _run_session_reminders(self) -> None:
        """DM the owner 30 min before each upcoming booking, and at start time."""
        await asyncio.sleep(30)
        tz = ZoneInfo(settings.padel_local_tz)
        from .booking import get_product_names
        while True:
            try:
                me_data = await self._fetch_my_state()
                _me, _slots, mine, names = me_data
                now = datetime.now(tz)
                owner = await self._owner()
                for b in mine:
                    start = b.start_dt.astimezone(tz)
                    delta = (start - now).total_seconds()
                    court = names.get(b.raw.get("productId"), "?")
                    # 30-min reminder window: 30 min ± 1 min
                    if 29 * 60 <= delta <= 31 * 60 and not self.rules_store.already_reminded(b.id, "30m"):
                        if owner:
                            try:
                                await owner.send(
                                    f":bell: **Padel in 30 min** — {court}, {start:%H:%M} (booking #{b.id})"
                                )
                                self.rules_store.mark_reminded(b.id, "30m")
                            except discord.HTTPException:
                                pass
                    # At start: from -1 min to +1 min
                    if -60 <= delta <= 60 and not self.rules_store.already_reminded(b.id, "start"):
                        if owner:
                            try:
                                await owner.send(
                                    f":tennis: **Padel starting now** — {court}, {start:%H:%M} (booking #{b.id})"
                                )
                                self.rules_store.mark_reminded(b.id, "start")
                            except discord.HTTPException:
                                pass
                # Also: refresh bookings message to mark a finished booking as past
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("reminder loop crashed")
                await asyncio.sleep(60)

    async def _owner(self) -> discord.User | None:
        if not settings.discord_owner_id:
            return None
        try:
            return await self.fetch_user(settings.discord_owner_id)
        except discord.NotFound:
            return None


def make_bot() -> PadelBot:
    store = RulesStore(settings.padelbot_rules_file)
    bot = PadelBot(store)
    _register_commands(bot)
    return bot


# --- slash commands ---


def _register_commands(bot: PadelBot) -> None:
    from .discord_ui import AutoBookView, BookingsView, DaySlotsView, SlotsRangeView, parse_day_range, today
    from datetime import timedelta

    @bot.tree.command(name='settings',description='Beheer de instellingen van de padelbot in deze server')
    @app_commands.allowed_installs(guilds=True,users=False)
    @app_commands.allowed_contexts(guilds=True,dms=False,private_channels=False)
    async def server_settings_cmd(interaction:discord.Interaction)->None:
        from .discord_sharing import installed_guild,with_server_settings
        if installed_guild(interaction) is None:
            await interaction.response.send_message('Gebruik /settings in een server waar de bot is toegevoegd.',ephemeral=True)
            return
        await interaction.response.send_message('Beheer delen en het deelkanaal op de website. Alleen serverbeheerders kunnen wijzigingen bewaren.',
            view=with_server_settings(None,interaction),ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

    @bot.tree.command(name='koppel',description='Koppel je Discord-account aan je Google-profiel in de padelhub')
    @app_commands.describe(code='Eenmalige code uit Instellingen op padel.sigmadvisor.com')
    async def link_profile(interaction:discord.Interaction,code:str):
        from .hub import HubStore
        try:
            if interaction.user.bot:raise ValueError('Gebruik je persoonlijke Discord-account.')
            hub=HubStore(bot.match_store)
            await asyncio.to_thread(hub.consume_code,code,interaction.user.id)
            from .identity import remember_interaction
            remember_interaction(hub,interaction)
            message='Discord is gekoppeld. De hub en Discord gebruiken nu dezelfde deelname. Vernieuw je profiel in de hub.'
        except ValueError as error:
            message=str(error)
        await interaction.response.send_message(message,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

    @bot.tree.command(name="checkbookings", description="Bekijk vrije padelvelden en boek via de knoppen")
    @app_commands.describe(dag="0 = vandaag, 1 = morgen, 2 = overmorgen; 0-5 = vandaag t/m over 5 dagen (standaard 0)")
    async def checkbookings_cmd(interaction: discord.Interaction, dag: str = "0") -> None:
        if bot.platform is not None:
            from .discord_platform import command
            try:first,last=parse_day_range(dag)
            except ValueError as error:
                await interaction.response.send_message(str(error),ephemeral=True);return
            await command(interaction,bot.platform,'slots',first,last);return
        if await _deny_if_not_owner(interaction):
            return
        try:
            first, last = parse_day_range(dag)
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            base = today()
            start = base + timedelta(days=first)
            if first == last:
                view = await DaySlotsView.load(settings.discord_owner_id, start)
            else:
                view = await SlotsRangeView.load(settings.discord_owner_id, start, base + timedelta(days=last))
        except Exception:
            logger.exception("Availability panel fetch failed")
            await interaction.followup.send("Beschikbaarheid ophalen mislukt. Probeer opnieuw.", ephemeral=True)
            return
        await interaction.followup.send(embed=view.embed(), view=view, ephemeral=True)

    @bot.tree.command(name="autobook", description="Stel automatisch boeken in of beheer je bestaande regels")
    async def autobook_cmd(interaction: discord.Interaction) -> None:
        if bot.platform is not None:
            from .discord_platform import command
            await command(interaction,bot.platform,'rules');return
        if await _deny_if_not_owner(interaction):
            return
        view = AutoBookView(settings.discord_owner_id, bot.rules_store)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @bot.tree.command(name="bookings", description="Bekijk je reservaties en deel een bevestigde boeking met de groep")
    async def bookings_cmd(interaction: discord.Interaction) -> None:
        if bot.platform is not None:
            from .discord_platform import command
            await command(interaction,bot.platform,'bookings');return
        if await _deny_if_not_owner(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            view = await BookingsView.load(settings.discord_owner_id)
        except Exception:
            logger.exception("Bookings panel fetch failed")
            await interaction.followup.send("Reservaties ophalen mislukt. Probeer opnieuw.", ephemeral=True)
            return
        await interaction.followup.send(embed=view.embed(), view=view, ephemeral=True)

    @bot.tree.command(name='deel',description='Deel je bevestigde padelboeking in deze chat')
    async def share_cmd(interaction:discord.Interaction)->None:
        if bot.platform is not None:
            from .discord_platform import command
            await command(interaction,bot.platform,'bookings');return
        await bookings_cmd.callback(interaction)

    @bot.tree.command(name="watcher", description="Volg vrije slots op een dag, of beheer je watchers")
    @app_commands.describe(dag="0 = vandaag, 1 = morgen; leeg laten om watchers te beheren",
        van="Vanaf dit uur (0–23, standaard 13)", tot="Tot en met dit startuur (1–24, standaard 20)")
    async def watcher_cmd(interaction: discord.Interaction, dag: app_commands.Range[int, 0, 365] | None = None,
        van: app_commands.Range[int, 0, 23] = 13, tot: app_commands.Range[int, 1, 24] = 20) -> None:
        if await _deny_if_not_owner(interaction):
            return
        from .watch_ui import WatchControlsView, WatchListView, watch_embed, remember_watch_interaction
        from .availability_watch import fetch_watch_day, snapshot_slots
        from . import weather
        await interaction.response.defer(thinking=True, ephemeral=True)
        if dag is None:
            view = WatchListView(bot.watch_store, settings.discord_owner_id)
            # Use a separate persistent instance: Discord gives ephemeral panels
            # a 15-minute lifetime even when their rendered view has timeout=None.
            bot.add_view(WatchListView(bot.watch_store, settings.discord_owner_id))
            for watch in view.watches:
                if watch.origin_channel_id == interaction.channel_id:
                    remember_watch_interaction(bot, bot.watch_store, watch, interaction)
            await interaction.edit_original_response(embed=view.embed(), view=view)
            return
        async with bot._availability_watch_lock:
            try:
                watch, created = bot.watch_store.create(settings.discord_owner_id, (today() + timedelta(days=dag)).date(), van, tot,
                    channel_id=interaction.channel_id, guild_id=interaction.guild_id)
            except ValueError as error:
                await interaction.followup.send(str(error), ephemeral=True)
                return
            try:
                slots, names = await asyncio.to_thread(fetch_watch_day, watch.day)
                snapshot = snapshot_slots(watch, slots)
                if created:
                    bot.watch_store.save_check(watch.id, snapshot)
            except Exception as error:
                logger.warning("Initial availability watch read failed: {}", type(error).__name__)
                bot.watch_store.save_check(watch.id, None, error="KUL ophalen mislukt; de watcher probeert opnieuw.")
                snapshot, names = watch.snapshot, {}
            forecast = await asyncio.to_thread(weather.get_forecast)
            watch = bot.watch_store.get(watch.id)
            remember_watch_interaction(bot, bot.watch_store, watch, interaction)
            bot.add_view(WatchControlsView(bot.watch_store, watch.id))
            bot.add_view(WatchListView(bot.watch_store, settings.discord_owner_id))
            await interaction.edit_original_response(content="✅ Watcher gestart in deze chat." if created else "Deze watcher is al actief in deze chat.",
                embed=watch_embed(watch, snapshot, names, forecast), view=WatchControlsView(bot.watch_store, watch.id))


def run() -> None:
    """Entry point for `padelbot discord`."""
    if not settings.discord_bot_token:
        sys.exit("DISCORD_BOT_TOKEN is not set in .env")
    if not settings.discord_owner_id:
        sys.exit("DISCORD_OWNER_ID is not set in .env")
    logger.remove()
    logger.add(sys.stderr, level=settings.log_level)
    from .runtime import instance_lock
    with instance_lock(settings.padelbot_rules_file.parent / "discord.lock") as acquired:
        if not acquired:
            logger.info("Discord bot already running; duplicate startup skipped.")
            return
        (settings.padelbot_rules_file.parent / "discord.pid").write_text(str(os.getpid()), encoding="utf-8")
        bot = make_bot()
        bot.run(settings.discord_bot_token)
