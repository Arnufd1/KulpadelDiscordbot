"""Discord bot — interactive control surface for the padel reservation bot.

Slash commands (owner-only):
  /slots day:YYYY-MM-DD                     — list available padel slots
  /book day:YYYY-MM-DD time:HH:MM           — book a single slot now
  /auto-add weekday time:HH:MM              — add a weekly recurring rule
  /auto-list                                 — list rules
  /auto-remove rule_id                      — remove a rule
  /auto-toggle rule_id enabled              — pause/resume a rule
  /status                                   — auth health, next fire, recent history
  /history                                  — last 10 booking attempts

Auth health is shown via /status. When the IdP session is dead, the bot DM's
the owner with a re-login reminder before the next scheduled fire.
"""
from __future__ import annotations
import asyncio
import sys
import time as _time
from datetime import datetime
from zoneinfo import ZoneInfo

from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from loguru import logger

from .auth import silent_refresh_via_session
from .booking import find_slots, find_slot_at_time, find_slots_window, get_product_names, book as do_book, BookingResult
from .booking_manager import cancel_booking, list_my_bookings
from .card_store import CardStore
from .client import BackboneClient
from .config import settings
from .live import SlotsView, render_bookings_embed, render_slots_embed
from .payment import PayMethod, auto_pay, has_direct_debit_mandate
from .rules_store import Rule, RulesStore, WEEKDAY_NAMES
from .scheduler import FireSpec, compute_next_fire, run_scheduler
from .store import TokenStore
from .watcher import WatchHit, run_watcher


WEEKDAY_CHOICES = [
    app_commands.Choice(name="Monday",    value=0),
    app_commands.Choice(name="Tuesday",   value=1),
    app_commands.Choice(name="Wednesday", value=2),
    app_commands.Choice(name="Thursday",  value=3),
    app_commands.Choice(name="Friday",    value=4),
    app_commands.Choice(name="Saturday",  value=5),
    app_commands.Choice(name="Sunday",    value=6),
]

COURT_CHOICES = [
    app_commands.Choice(name="Padel 1", value=1),
    app_commands.Choice(name="Padel 2", value=2),
    app_commands.Choice(name="Padel 3", value=3),
    app_commands.Choice(name="Padel 4", value=4),
    app_commands.Choice(name="Padel 5", value=5),
]


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
        super().__init__(command_prefix="!padelbot ", intents=intents)
        self.rules_store = store
        self._scheduler_task: asyncio.Task | None = None
        self._watcher_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._slots_refresh_task: asyncio.Task | None = None
        self._bookings_refresh_task: asyncio.Task | None = None
        self._reminder_task: asyncio.Task | None = None
        self._last_keepalive_ok: bool = True
        self._last_keepalive_at: float = 0.0

    async def setup_hook(self) -> None:
        logger.info("setup_hook: syncing slash commands...")
        try:
            if settings.discord_guild_id:
                guild = discord.Object(id=settings.discord_guild_id)
                self.tree.copy_global_to(guild=guild)
                cmds = await self.tree.sync(guild=guild)
                logger.info("Synced {} slash commands to guild {}.", len(cmds), settings.discord_guild_id)
            else:
                cmds = await self.tree.sync()
                logger.info("Synced {} slash commands globally (may take up to 1h).", len(cmds))
        except Exception:
            logger.exception("Failed to sync slash commands")
            raise

        # Register the persistent SlotsView so day-buttons keep working across restarts
        if settings.discord_owner_id:
            self.add_view(SlotsView(settings.discord_owner_id))

    async def on_ready(self) -> None:
        logger.info("Bot ready as {} (id={}).", self.user, getattr(self.user, "id", "?"))
        owner_ok = bool(settings.discord_owner_id)
        guild_ok = bool(settings.discord_guild_id)
        logger.info(
            "Owner gating: {} · Guild scope: {} · Type /status in Discord to test.",
            "ON" if owner_ok else "DISABLED — anyone can use commands!",
            f"guild={settings.discord_guild_id}" if guild_ok else "global",
        )

        # on_ready can fire multiple times on reconnect — only start tasks once.
        if self._scheduler_task is not None and not self._scheduler_task.done():
            return

        # Start the precise scheduler (Type A: weekly opening moment)
        self._scheduler_task = asyncio.create_task(
            run_scheduler(self.rules_store, on_attempt=self._announce_attempt)
        )
        # Start the keepalive task — periodic silent refresh to keep the
        # Shibboleth session warm and avoid idle-timeout MFA prompts.
        self._keepalive_task = asyncio.create_task(self._run_keepalive())

        # Live channel refreshers (only run if their channel is configured)
        self._slots_refresh_task = asyncio.create_task(self._run_slots_refresh())
        self._bookings_refresh_task = asyncio.create_task(self._run_bookings_refresh())
        self._reminder_task = asyncio.create_task(self._run_session_reminders())

        # Start the slot watcher (Type B: catches cancellations within the
        # 7-day window, auto-books if a watched slot frees up).
        self._watcher_task = asyncio.create_task(
            run_watcher(
                self.rules_store,
                on_hit=self._on_watch_hit,
                poll_interval_s=settings.padelbot_watcher_poll_s,
            )
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
            if result.paid:
                paid_line = f":white_check_mark: Paid via **{result.pay_method}**."
            elif result.payment_url:
                paid_line = (
                    f":credit_card: **Click to pay** (€{result.amount}, 60-min window):\n"
                    f"{result.payment_url}"
                )
            else:
                paid_line = (
                    f":warning: Payment NOT auto-completed: `{result.pay_error or 'unknown'}`. "
                    f"Pay in the KU Leuven Sport app within 60 min."
                )
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
        first failure (MFA WebSocket triggered = session dead), DMs the owner
        and stops itself so we don't keep poking a dead session.
        """
        import time as _time
        # First refresh after a short delay so we don't hit the API the moment
        # the bot starts (the user may have just done /login).
        await asyncio.sleep(60)
        interval = settings.padelbot_keepalive_interval_s
        while True:
            try:
                def _do_refresh() -> tuple[bool, str | None]:
                    try:
                        from .auth import silent_refresh_via_session
                        from .store import TokenStore
                        bundle = silent_refresh_via_session(settings.padelbot_storage_file)
                        TokenStore(settings.padelbot_key_file, settings.padelbot_token_file).save(bundle)
                        return True, None
                    except Exception as e:
                        return False, f"{type(e).__name__}: {e}"

                ok, err = await asyncio.to_thread(_do_refresh)
                self._last_keepalive_ok = ok
                self._last_keepalive_at = _time.time()
                if ok:
                    logger.info("Keepalive: silent refresh OK.")
                else:
                    logger.warning("Keepalive: silent refresh FAILED: {}", err)
                    owner = await self._owner()
                    if owner:
                        try:
                            await owner.send(
                                ":warning: **Padel bot — session expired.**\n"
                                f"Silent refresh failed: `{err}`.\n"
                                "SSH into the Pi and run `padelbot login` (one MFA tap), "
                                "then restart `padelbot discord` so keepalive resumes."
                            )
                        except discord.HTTPException as e:
                            logger.warning("Could not DM owner about session death: {}", e)
                    return  # stop trying — wait for user to re-login + restart

                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                logger.info("Keepalive stopping.")
                raise
            except Exception:
                logger.exception("Keepalive loop crashed — sleeping {}s before retry", interval)
                await asyncio.sleep(interval)

    async def _on_watch_hit(self, hit: WatchHit) -> None:
        """A watched slot just became available within the 7-day window
        (most likely someone cancelled). Auto-book and DM the owner."""
        owner = await self._owner()
        target_iso = hit.target_slot_local.isoformat()

        def _book_now() -> tuple[bool, BookingResult | None, str | None]:
            try:
                with BackboneClient() as c:
                    me = c.me()
                    res = do_book(c, hit.slot, member_id=me["id"], me=me, label=hit.rule.notes or None)
                return True, res, None
            except Exception as e:
                return False, None, f"{type(e).__name__}: {e}"

        ok, res, err = await asyncio.to_thread(_book_now)
        self.rules_store.record(
            rule_id=hit.rule.id,
            success=ok,
            sale_id=(res.sale_id if res else None),
            booking_id=(res.booking_id if res else None),
            target_slot_iso=target_iso,
            error=err,
        )
        if owner is None:
            return
        if ok and res:
            paid_line = (
                f"\nPaid via **{res.pay_method}**." if res.paid
                else f"\n:warning: Payment NOT auto-completed: `{res.pay_error or 'unknown'}`. Pay in the app."
            )
            msg = (
                f":eyes: **Watched slot opened up — booked!**\n"
                f"Rule `{hit.rule.label()}` for slot {hit.target_slot_local:%Y-%m-%d %H:%M %Z}\n"
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

    # ---- live channel refreshers ----

    async def _fetch_my_state(self):
        """Pulls slots+bookings+names. The sync API work runs in a worker thread."""
        def _sync():
            from .booking import find_slots_window, get_product_names
            from datetime import datetime as _dt
            with BackboneClient() as c:
                me = c.me()
                slots = find_slots_window(c, start_date=_dt.now(), days=8)
                mine = list_my_bookings(c, member_id=me["id"], upcoming_only=True)
                court_ids = {s.bookable_product_id for s in slots} | {
                    b.raw.get("productId") for b in mine if b.raw.get("productId")
                }
                names = get_product_names(c, {cid for cid in court_ids if cid}) if court_ids else {}
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
            me, slots, _mine, names = await self._fetch_my_state()
        except Exception:
            logger.exception("slots refresh fetch failed")
            return
        embed = render_slots_embed(slots, names, member_name=f"{me.get('firstName')} {me.get('lastName')}")
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


# --- helpers used inside slash commands ---

def _local_now() -> datetime:
    return datetime.now(ZoneInfo(settings.padel_local_tz))


def _check_auth_state() -> tuple[bool, str]:
    """Returns (ok, message). Tries silent-refresh to verify auth is alive."""
    s = TokenStore(settings.padelbot_key_file, settings.padelbot_token_file)
    bundle = s.load()
    if bundle is None:
        return False, "no tokens stored — run `padelbot login`"
    if bundle.expires_at > _time.time():
        return True, "access token still valid"
    try:
        new_bundle = silent_refresh_via_session(settings.padelbot_storage_file)
        s.save(new_bundle)
        return True, "silent refresh OK"
    except RuntimeError as e:
        return False, str(e)


# --- command registration ---

def _register_commands(bot: PadelBot) -> None:
    tree = bot.tree

    @tree.command(name="status", description="Show bot health, auth state, next fire, recent history")
    async def status_cmd(interaction: discord.Interaction) -> None:
        if await _deny_if_not_owner(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        rules = bot.rules_store.list_rules()
        next_fire_str = "(no enabled rules)"
        enabled_rules = [r for r in rules if r.enabled]
        if enabled_rules:
            specs = [compute_next_fire(r) for r in enabled_rules]
            specs.sort(key=lambda s: s.fire_at)
            n = specs[0]
            next_fire_str = (
                f"`{n.rule.label()}` → slot {n.target_slot_local:%Y-%m-%d %H:%M %Z}, "
                f"firing at {n.fire_at:%Y-%m-%d %H:%M:%S %Z}"
            )
        # Auth check (may launch headless Playwright — do it in a thread)
        auth_ok, auth_msg = await asyncio.to_thread(_check_auth_state)
        history = bot.rules_store.recent_history(limit=5)
        hist_str = "\n".join(
            f"  {h.attempted_at} {'OK' if h.success else 'FAIL'} "
            f"sale={h.sale_id} booking={h.booking_id} {h.error or ''}"
            for h in history
        ) or "  (none)"
        # Keepalive status
        import time as _time
        if bot._last_keepalive_at == 0:
            ka_str = "not run yet (first refresh after 60s of bot start)"
        else:
            secs_ago = int(_time.time() - bot._last_keepalive_at)
            ka_str = f"{'OK' if bot._last_keepalive_ok else 'FAILED'} {secs_ago}s ago (interval {settings.padelbot_keepalive_interval_s}s)"

        embed = discord.Embed(title="Padel bot status", color=0x00ff00 if auth_ok else 0xff0000)
        embed.add_field(name="Auth", value=f"{'OK' if auth_ok else 'DEAD'}: {auth_msg}", inline=False)
        embed.add_field(name="Keepalive", value=ka_str, inline=False)
        embed.add_field(name="Rules", value=f"{len(enabled_rules)}/{len(rules)} enabled", inline=False)
        embed.add_field(name="Next fire", value=next_fire_str, inline=False)
        embed.add_field(name="Recent history", value=f"```{hist_str}```", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @tree.command(name="slots", description="List padel slots for a date")
    @app_commands.describe(day="Target date YYYY-MM-DD (Brussels time)")
    async def slots_cmd(interaction: discord.Interaction, day: str) -> None:
        if await _deny_if_not_owner(interaction):
            return
        await interaction.response.defer(thinking=True)
        try:
            target = datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            await interaction.followup.send(":x: bad date — use YYYY-MM-DD")
            return

        def fetch():
            with BackboneClient() as c:
                return c.me(), find_slots(c, target_date=target)
        try:
            me, slots = await asyncio.to_thread(fetch)
        except Exception as e:
            await interaction.followup.send(f":x: API call failed: `{type(e).__name__}: {e}`")
            return

        if not slots:
            await interaction.followup.send(f":calendar_spiral: No padel slots on {day}")
            return

        tz_offset = int(_local_now().utcoffset().total_seconds() // 3600)  # type: ignore[union-attr]
        embed = discord.Embed(
            title=f"Padel slots — {day}",
            description=f"Logged in as {me.get('firstName')} {me.get('lastName')}",
            color=0x377dff,
        )
        for s in slots[:24]:  # Discord field limit is 25
            local_h = (s.start_dt.hour + tz_offset) % 24
            tlabel = f"{local_h:02d}:{s.start_dt.minute:02d}"
            avail = s.raw.get("availableParticipantCount")
            max_p = s.raw.get("maxParticipants")
            embed.add_field(
                name=tlabel,
                value=f"avail `{avail}/{max_p}`\nslot `{s.bookable_linked_product_id}`",
                inline=True,
            )
        if len(slots) > 24:
            embed.set_footer(text=f"{len(slots) - 24} more slot(s) not shown.")
        await interaction.followup.send(embed=embed)

    @tree.command(name="book", description="Book a single padel slot now")
    @app_commands.describe(
        day="Date YYYY-MM-DD (Brussels)",
        time="Local start time HH:MM (Brussels)",
        court="Optional: prefer a specific court (otherwise any available)",
        label="Optional: free-text note attached to the booking (e.g. 'for Sara & Tom')",
    )
    @app_commands.choices(court=COURT_CHOICES)
    async def book_cmd(
        interaction: discord.Interaction,
        day: str,
        time: str,
        court: app_commands.Choice[int] | None = None,
        label: str | None = None,
    ) -> None:
        if await _deny_if_not_owner(interaction):
            return
        await interaction.response.defer(thinking=True)
        try:
            target = datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            await interaction.followup.send(":x: bad date — use YYYY-MM-DD")
            return

        court_value = court.value if court else None

        def attempt():
            with BackboneClient() as c:
                me = c.me()
                slots = find_slots(c, target_date=target)
                names = get_product_names(c, {s.bookable_product_id for s in slots})
                slot = find_slot_at_time(slots, time, court=court_value, court_names=names)
                if slot is None:
                    return None, slots, None, names
                return slot, None, do_book(c, slot, member_id=me["id"], me=me, label=label), names

        try:
            slot, slots, result, names = await asyncio.to_thread(attempt)
        except Exception as e:
            await interaction.followup.send(f":x: booking failed: `{type(e).__name__}: {e}`")
            return

        if result is None:
            tz = ZoneInfo(settings.padel_local_tz)
            opts = []
            for s in (slots or []):
                local = s.start_dt.astimezone(tz)
                opts.append(f"{local:%H:%M} {names.get(s.bookable_product_id, '?')}")
            avail = ", ".join(opts) or "(none)"
            court_msg = f" on {court.name}" if court else ""
            await interaction.followup.send(
                f":x: No slot at {time}{court_msg} on {day}.\nAvailable: {avail[:1500]}"
            )
            return

        paid_line = (
            f":white_check_mark: Paid via **{result.pay_method}**."
            if result.paid else
            f":warning: Payment NOT auto-completed: `{result.pay_error or 'unknown'}`. Pay in the KU Leuven Sport app within ~15 min."
        )
        msg = (
            f":tennis: **Booked!** {result.description}\n"
            f"sale_id `{result.sale_id}` booking_id `{result.booking_id}` "
            f"amount `{result.amount} EUR`\n"
            f"{paid_line}"
        )
        await interaction.followup.send(msg)

    @tree.command(name="auto-add", description="Add a weekly recurring booking rule")
    @app_commands.describe(weekday="Day of week", time="Local start time HH:MM (Brussels)", notes="Optional notes")
    @app_commands.choices(weekday=WEEKDAY_CHOICES)
    async def auto_add_cmd(
        interaction: discord.Interaction,
        weekday: app_commands.Choice[int],
        time: str,
        notes: str | None = None,
    ) -> None:
        if await _deny_if_not_owner(interaction):
            return
        if len(time) != 5 or time[2] != ":":
            await interaction.response.send_message(":x: time must be HH:MM", ephemeral=True)
            return
        rule = bot.rules_store.add_rule(weekday.value, time, notes)
        spec = compute_next_fire(rule)
        await interaction.response.send_message(
            f":white_check_mark: Added rule `#{rule.id}`: **{rule.label()}**\n"
            f"Next fire: {spec.fire_at:%Y-%m-%d %H:%M:%S %Z} (slot {spec.target_slot_local:%Y-%m-%d %H:%M})",
            ephemeral=True,
        )

    @tree.command(name="auto-list", description="List all recurring rules")
    async def auto_list_cmd(interaction: discord.Interaction) -> None:
        if await _deny_if_not_owner(interaction):
            return
        rules = bot.rules_store.list_rules()
        if not rules:
            await interaction.response.send_message("No rules. Use `/auto-add`.", ephemeral=True)
            return
        lines = []
        for r in rules:
            try:
                spec = compute_next_fire(r)
                next_str = f"  next: {spec.fire_at:%Y-%m-%d %H:%M %Z} → slot {spec.target_slot_local:%Y-%m-%d %H:%M}"
            except Exception as e:
                next_str = f"  (could not compute next fire: {e})"
            tag = "" if r.enabled else " (disabled)"
            notes = f"  notes: {r.notes}" if r.notes else ""
            lines.append(f"#{r.id}  {r.label()}{tag}\n{next_str}{notes}")
        await interaction.response.send_message("```\n" + "\n\n".join(lines) + "\n```", ephemeral=True)

    @tree.command(name="auto-remove", description="Remove a recurring rule by id")
    @app_commands.describe(rule_id="Rule id from /auto-list")
    async def auto_remove_cmd(interaction: discord.Interaction, rule_id: int) -> None:
        if await _deny_if_not_owner(interaction):
            return
        ok = bot.rules_store.remove_rule(rule_id)
        if ok:
            await interaction.response.send_message(f":white_check_mark: Removed rule `#{rule_id}`.", ephemeral=True)
        else:
            await interaction.response.send_message(f":x: No rule `#{rule_id}`.", ephemeral=True)

    @tree.command(name="auto-toggle", description="Enable or disable a rule")
    @app_commands.describe(rule_id="Rule id", enabled="True to enable, False to disable")
    async def auto_toggle_cmd(interaction: discord.Interaction, rule_id: int, enabled: bool) -> None:
        if await _deny_if_not_owner(interaction):
            return
        ok = bot.rules_store.set_enabled(rule_id, enabled)
        await interaction.response.send_message(
            f":white_check_mark: Rule `#{rule_id}` set to {'enabled' if enabled else 'disabled'}." if ok
            else f":x: No rule `#{rule_id}`.",
            ephemeral=True,
        )

    @tree.command(name="week", description="All available padel slots in the next 7 days")
    @app_commands.describe(
        start="Optional start date YYYY-MM-DD (default: today)",
        court="Optional: filter to one court only",
    )
    @app_commands.choices(court=COURT_CHOICES)
    async def week_cmd(
        interaction: discord.Interaction,
        start: str | None = None,
        court: app_commands.Choice[int] | None = None,
    ) -> None:
        if await _deny_if_not_owner(interaction):
            return
        await interaction.response.defer(thinking=True)

        tz = ZoneInfo(settings.padel_local_tz)
        try:
            start_dt = (
                datetime.strptime(start, "%Y-%m-%d") if start
                else datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)
            )
        except ValueError:
            await interaction.followup.send(":x: bad date — use YYYY-MM-DD")
            return

        def fetch():
            with BackboneClient() as c:
                me = c.me()
                slots = find_slots_window(c, start_date=start_dt, days=7)
                court_ids = {s.bookable_product_id for s in slots}
                names = get_product_names(c, court_ids)
                return me, slots, names

        try:
            me, slots, court_names = await asyncio.to_thread(fetch)
        except Exception as e:
            await interaction.followup.send(f":x: API call failed: `{type(e).__name__}: {e}`")
            return

        if not slots:
            await interaction.followup.send("No padel slots in the next 7 days.")
            return

        # Apply court filter if given
        if court is not None:
            target_name = f"padel {court.value}".lower()
            filtered = [
                s for s in slots
                if (court_names.get(s.bookable_product_id) or "").strip().lower() == target_name
            ]
            slots = filtered
            if not slots:
                await interaction.followup.send(
                    f"No slots available on {court.name} in the next 7 days."
                )
                return

        # Build the grid keyed by (day, time, court).
        all_courts: set[int] = {s.bookable_product_id for s in slots}
        courts_sorted = sorted(all_courts, key=lambda c: court_names.get(c, str(c)))

        by_day: dict[str, dict[str, set[int]]] = {}  # day -> time -> set of court ids
        for s in slots:
            local = s.start_dt.astimezone(tz)
            day_label = local.strftime("%a %d %b")
            t = local.strftime("%H:%M")
            by_day.setdefault(day_label, {}).setdefault(t, set()).add(s.bookable_product_id)

        def col_label(cid: int) -> str:
            n = court_names.get(cid, f"#{cid}")
            if n.lower().startswith("padel"):
                rest = n[5:].strip()
                return f"P{rest}" if rest else "P?"
            return n[:4]

        # Discord ANSI: bold green for available, faint dark for unavailable.
        GREEN = "[1;32m"
        GRAY = "[2;30m"
        RESET = "[0m"

        # Per-day holiday/closure metadata from any one slot of that day.
        day_meta: dict[str, tuple[bool, bool]] = {}  # date_label -> (is_holiday, day_has_bookings)
        for s in slots:
            local = s.start_dt.astimezone(tz)
            day_label = local.strftime("%a %d %b")
            day_meta[day_label] = (s.is_holiday, s.day_has_bookings)

        chunks = []
        for day_label, time_map in by_day.items():
            times_today = sorted(time_map.keys())
            total_open = sum(len(time_map[t]) for t in times_today)
            is_hol, has_bk = day_meta.get(day_label, (False, False))
            tag = ""
            if is_hol and not has_bk:
                tag = " :warning: **HOLIDAY — likely closed (no bookings)**"
            elif is_hol and has_bk:
                tag = " :tada: holiday but appears open"
            rows = []
            for t in times_today:
                cells = []
                for c in courts_sorted:
                    label = col_label(c)
                    if c in time_map[t]:
                        cells.append(f"{GREEN}{label}{RESET}")
                    else:
                        cells.append(f"{GRAY}{label}{RESET}")
                rows.append(f"{t}  " + "  ".join(cells))
            block = f"**{day_label}** — {total_open} open{tag}\n```ansi\n" + "\n".join(rows) + "\n```"
            chunks.append(block)

        legend = "  ".join(f"{col_label(c)}={court_names.get(c, f'#{c}')}" for c in courts_sorted)
        # Send each day as its own embed so we never run out of description space.
        # First embed gets a header; others have just their day grid.
        first = True
        for day_label, block in zip(by_day.keys(), chunks):
            if first:
                e = discord.Embed(
                    title=f"Padel — 7-day grid from {start_dt:%Y-%m-%d}",
                    description=block,
                    color=0x377dff,
                )
                e.set_footer(text=f"{len(slots)} open slots · {legend}")
                first = False
            else:
                e = discord.Embed(description=block, color=0x377dff)
            await interaction.followup.send(embed=e)


    @tree.command(name="bookings", description="List your upcoming padel bookings")
    async def bookings_cmd(interaction: discord.Interaction) -> None:
        if await _deny_if_not_owner(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)

        def fetch():
            with BackboneClient() as c:
                me = c.me()
                mine = list_my_bookings(c, member_id=me["id"], upcoming_only=True)
                # Resolve court names for the productIds in the bookings.
                court_pids = {b.raw.get("productId") for b in mine if b.raw.get("productId")}
                names = get_product_names(c, court_pids) if court_pids else {}
                return me, mine, names

        try:
            me, mine, court_names = await asyncio.to_thread(fetch)
        except Exception as e:
            await interaction.followup.send(f":x: API call failed: `{type(e).__name__}: {e}`", ephemeral=True)
            return

        if not mine:
            await interaction.followup.send("No upcoming bookings.", ephemeral=True)
            return

        tz = ZoneInfo(settings.padel_local_tz)
        GREEN = "\x1b[1;32m"
        DIM = "\x1b[2;37m"
        RESET = "\x1b[0m"

        # Build an ANSI-coloured block where each booking's day+time+court pops in green.
        lines = []
        for b in mine[:25]:
            local = b.start_dt.astimezone(tz)
            court_name = court_names.get(b.raw.get("productId"), "?")
            paid_word = "PAID" if b.paid_for else "UNPAID"
            lines.append(
                f"{GREEN}{local:%a %d %b  %H:%M}  {court_name}{RESET}\n"
                f"{DIM}#{b.id}  sale {b.sale_id}  {paid_word}{RESET}"
            )
        body = "```ansi\n" + "\n\n".join(lines) + "\n```"
        embed = discord.Embed(
            title=f"Upcoming bookings — {me.get('firstName')} {me.get('lastName')}",
            description=body,
            color=0x00bb00,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @tree.command(name="cancel", description="Cancel one of your bookings by id")
    @app_commands.describe(booking_id="From /bookings")
    async def cancel_cmd(interaction: discord.Interaction, booking_id: int) -> None:
        if await _deny_if_not_owner(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)

        def attempt():
            with BackboneClient() as c:
                me = c.me()
                return cancel_booking(c, booking_id=booking_id, member_id=me["id"])

        try:
            resp = await asyncio.to_thread(attempt)
            await interaction.followup.send(
                f":white_check_mark: Cancelled booking `{booking_id}`. Response: ```{str(resp)[:1500]}```",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(
                f":x: Cancel failed: `{type(e).__name__}: {e}`", ephemeral=True
            )

    @tree.command(name="card-status", description="Show payment configuration (mandate / saved card)")
    async def card_status_cmd(interaction: discord.Interaction) -> None:
        if await _deny_if_not_owner(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            with BackboneClient() as c:
                me = await asyncio.to_thread(c.me)
        except Exception as e:
            await interaction.followup.send(f":x: API call failed: `{type(e).__name__}: {e}`", ephemeral=True)
            return

        store = CardStore(settings.padelbot_key_file, settings.padelbot_card_file)
        try:
            card = store.load()
        except FileNotFoundError:
            card = None

        lines = []
        if has_direct_debit_mandate(me):
            lines.append(f":bank: SEPA mandate: **active** (`{me.get('directDebitMandate')}`)")
            lines.append("Auto-payments will use **direct debit** (no card needed).")
        else:
            lines.append(":warning: SEPA mandate: **not set up**")
        if card:
            lines.append(f":credit_card: Saved card: `{card.masked()}`")
        else:
            lines.append(":credit_card: Saved card: none (set on Pi via `padelbot set-card`)")
        lines.append(f":coin: Account balance: `{me.get('balance')} EUR`")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @tree.command(name="pay", description="Trigger payment for an unpaid booking using best available method")
    @app_commands.describe(sale_id="Sale id from /bookings or after a booking")
    async def pay_cmd(interaction: discord.Interaction, sale_id: int) -> None:
        if await _deny_if_not_owner(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)

        def attempt():
            with BackboneClient() as c:
                me = c.me()
                # Look up the sale to find its amount
                sale = c.get(f"/sales/{sale_id}")
                amount = (sale.get("data") or sale).get("total") if isinstance(sale, dict) else None
                if amount is None:
                    raise RuntimeError(f"Could not determine sale amount: {sale!r}")
                method, resp = auto_pay(
                    c, sale_id=sale_id, member_id=me["id"], amount=float(amount), me=me,
                )
                return method, amount, resp

        try:
            method, amount, _ = await asyncio.to_thread(attempt)
            await interaction.followup.send(
                f":white_check_mark: Paid sale `{sale_id}` (`{amount} EUR`) via **{method.name}**.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(
                f":x: Payment failed: `{type(e).__name__}: {e}`", ephemeral=True
            )

    @tree.command(name="setup-slots-here", description="Post the live slots message in this channel and remember it")
    async def setup_slots_cmd(interaction: discord.Interaction) -> None:
        if await _deny_if_not_owner(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            me, slots, _mine, names = await bot._fetch_my_state()
        except Exception as e:
            await interaction.followup.send(f":x: API error: `{type(e).__name__}: {e}`", ephemeral=True)
            return
        embed = render_slots_embed(slots, names, member_name=f"{me.get('firstName')} {me.get('lastName')}")
        view = SlotsView(settings.discord_owner_id)
        msg = await interaction.channel.send(embed=embed, view=view)
        bot.rules_store.kv_set("slots_channel_id", str(interaction.channel_id))
        bot.rules_store.kv_set("slots_message_id", str(msg.id))
        await interaction.followup.send(
            f":white_check_mark: Slots message posted: {msg.jump_url}\nWill auto-refresh every 5 min.",
            ephemeral=True,
        )

    @tree.command(name="setup-bookings-here", description="Post the live bookings message in this channel")
    async def setup_bookings_cmd(interaction: discord.Interaction) -> None:
        if await _deny_if_not_owner(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            me, _slots, mine, names = await bot._fetch_my_state()
        except Exception as e:
            await interaction.followup.send(f":x: API error: `{type(e).__name__}: {e}`", ephemeral=True)
            return
        embed = render_bookings_embed(mine, names, member_name=f"{me.get('firstName')} {me.get('lastName')}")
        msg = await interaction.channel.send(embed=embed)
        bot.rules_store.kv_set("bookings_channel_id", str(interaction.channel_id))
        bot.rules_store.kv_set("bookings_message_id", str(msg.id))
        await interaction.followup.send(
            f":white_check_mark: Bookings message posted: {msg.jump_url}\nWill auto-refresh every 10 min.",
            ephemeral=True,
        )

    @tree.command(name="history", description="Show last 10 booking attempts")
    async def history_cmd(interaction: discord.Interaction) -> None:
        if await _deny_if_not_owner(interaction):
            return
        rows = bot.rules_store.recent_history(limit=10)
        if not rows:
            await interaction.response.send_message("No history yet.", ephemeral=True)
            return
        lines = [
            f"{r.attempted_at}  {'OK ' if r.success else 'FAIL'}  "
            f"rule={r.rule_id} sale={r.sale_id} booking={r.booking_id}  "
            f"slot={r.target_slot_iso or '?'}"
            + (f"\n   err={r.error[:200]}" if r.error else "")
            for r in rows
        ]
        await interaction.response.send_message("```\n" + "\n".join(lines) + "\n```", ephemeral=True)


def run() -> None:
    """Entry point for `padelbot discord`."""
    if not settings.discord_bot_token:
        sys.exit("DISCORD_BOT_TOKEN is not set in .env")
    if not settings.discord_owner_id:
        sys.exit("DISCORD_OWNER_ID is not set in .env")
    logger.remove()
    logger.add(sys.stderr, level=settings.log_level)
    bot = make_bot()
    bot.run(settings.discord_bot_token)
