"""Discord live-updating messages for slots and bookings, plus interactive views.

The slots message uses a persistent View with 7 day-buttons (Mon..Sun, by
weekday — labels updated each render to show the actual date). Clicking a
button sends an ephemeral select menu with up to 25 (time, court) options for
that day. Selecting one triggers the booking + DM confirmation.
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from loguru import logger

from .booking import (
    BookingResult,
    Slot,
    book as do_book,
    find_slot_at_time,
    find_slots_window,
    get_product_names,
)
from .booking_manager import list_my_bookings
from .client import BackboneClient
from .config import settings


GREEN = "\x1b[1;32m"
GRAY = "\x1b[2;30m"
DIM = "\x1b[2;37m"
RESET = "\x1b[0m"


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.padel_local_tz)


# ------------ rendering ------------

WEEKLY_BOOKING_QUOTA = 2  # KU Leuven Sport limit for padel students


def render_slots_embed(
    slots: list[Slot],
    court_names: dict[int, str],
    *,
    member_name: str = "",
    num_days: int = 8,
    my_booking_days: set[str] | None = None,
    my_bookings_count: int = 0,
) -> discord.Embed:
    """One embed field per day with the bookable courts.

    Days where the user already has a booking are HIDDEN (KUL blocks
    another booking that day). When the user has hit the weekly quota
    (`my_bookings_count >= WEEKLY_BOOKING_QUOTA`), no slots are shown
    and the embed just explains the situation.
    """
    tz = _tz()
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    my_booking_days = my_booking_days or set()

    by_day: dict[str, dict[str, set[int]]] = {}  # day -> time -> set of court parents
    day_meta: dict[str, tuple[bool, bool]] = {}
    all_courts: set[int] = set()
    for s in slots:
        local = s.start_dt.astimezone(tz)
        day_label = local.strftime("%a %d %b")
        by_day.setdefault(day_label, {}).setdefault(local.strftime("%H:%M"), set()).add(s.bookable_product_id)
        day_meta[day_label] = (s.is_holiday, s.day_has_bookings)
        all_courts.add(s.bookable_product_id)

    courts_sorted = sorted(all_courts, key=lambda c: court_names.get(c, str(c)))

    def col_label(cid: int) -> str:
        n = court_names.get(cid, f"#{cid}")
        if n.lower().startswith("padel"):
            rest = n[5:].strip()
            return f"P{rest}" if rest else "P?"
        return n[:4]

    embed = discord.Embed(title="🎾 Padel — next 8 days", color=0x377dff, timestamp=datetime.now(tz))

    # Quota banner — when user is at the weekly cap, no slot is bookable.
    if my_bookings_count >= WEEKLY_BOOKING_QUOTA:
        embed.add_field(
            name=f"🚫 Weekly quota reached ({my_bookings_count}/{WEEKLY_BOOKING_QUOTA})",
            value=(
                "KU Leuven Sport limits students to "
                f"**{WEEKLY_BOOKING_QUOTA} padel reservations per week**. "
                "Cancel one with `/cancel booking_id:N` to free a slot "
                "(find ids with `/bookings`)."
            ),
            inline=False,
        )
        embed.set_footer(text=(member_name + " · " if member_name else "") + "no bookable slots until quota frees up")
        return embed

    total_slots = 0
    # Reverse order: latest day at top, today at bottom.
    for offset in reversed(range(num_days)):
        d = today + timedelta(days=offset)
        day_label = d.strftime("%a %d %b")
        time_map = by_day.get(day_label, {})
        is_hol, has_bk = day_meta.get(day_label, (False, False))
        day_key = d.strftime("%Y-%m-%d")

        # Hide days where the user already has a booking — KUL blocks another.
        if day_key in my_booking_days:
            continue

        n_open = sum(len(v) for v in time_map.values())
        total_slots += n_open

        tag = ""
        if is_hol and not has_bk:
            tag = " ⚠️"
        elif is_hol and has_bk:
            tag = " 🎉"

        if not time_map:
            value = "*no slots*"
        else:
            rows = []
            for t in sorted(time_map.keys()):
                cells = []
                for c in courts_sorted:
                    lab = col_label(c)
                    if c in time_map[t]:
                        cells.append(f"{GREEN}{lab}{RESET}")
                    else:
                        cells.append(f"{GRAY}{lab}{RESET}")
                rows.append(f"{t} " + " ".join(cells))
            value = "```ansi\n" + "\n".join(rows) + "\n```"
            if len(value) > 1020:
                value = value[:1015] + "\n```"

        embed.add_field(name=f"{day_label} — {n_open}{tag}", value=value, inline=False)

    refresh_min = max(1, settings.padelbot_slots_refresh_s // 60)
    legend = "  ".join(f"{col_label(c)}={court_names.get(c, str(c))}" for c in courts_sorted)
    quota_str = f"quota {my_bookings_count}/{WEEKLY_BOOKING_QUOTA}"
    footer = f"{total_slots} open · {quota_str} · {legend} · refresh every {refresh_min} min · click day to book"
    if member_name:
        footer = f"{member_name} · " + footer
    embed.set_footer(text=footer[:2000])
    return embed


def render_bookings_embed(my_bookings, court_names: dict[int, str], member_name: str = "") -> discord.Embed:
    tz = _tz()
    if not my_bookings:
        embed = discord.Embed(
            title="🎾 Upcoming bookings",
            description="*(none)*",
            color=0x00bb00,
            timestamp=datetime.now(tz),
        )
        if member_name:
            embed.set_footer(text=member_name)
        return embed

    lines = []
    for b in my_bookings[:25]:
        local = b.start_dt.astimezone(tz)
        court = court_names.get(b.raw.get("productId"), "?")
        paid = "PAID" if b.paid_for else "UNPAID"
        lines.append(
            f"{GREEN}{local:%a %d %b  %H:%M}  {court}{RESET}\n"
            f"{DIM}#{b.id}  sale {b.sale_id}  {paid}{RESET}"
        )
    body = "```ansi\n" + "\n\n".join(lines) + "\n```"
    embed = discord.Embed(
        title=f"🎾 Upcoming bookings ({len(my_bookings)})",
        description=body,
        color=0x00bb00,
        timestamp=datetime.now(tz),
    )
    if member_name:
        embed.set_footer(text=member_name)
    return embed


# ------------ interactive view ------------

def _today_local_date() -> datetime:
    return datetime.now(_tz()).replace(hour=0, minute=0, second=0, microsecond=0)


class SlotsView(discord.ui.View):
    """Persistent view with 8 buttons (today + next 7 days). 2 rows of 4."""

    def __init__(self, owner_id: int):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        for offset in range(8):
            self.add_item(_DayButton(offset, owner_id))


class _DayButton(discord.ui.Button):
    def __init__(self, offset: int, owner_id: int):
        d = _today_local_date() + timedelta(days=offset)
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=d.strftime("%a %d/%m"),
            custom_id=f"padelbot:book_day:{offset}",
            row=offset // 4,  # 4 buttons per row -> 2 rows for 8 buttons
        )
        self._offset = offset
        self.owner_id = owner_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                ":no_entry_sign: This bot is private.", ephemeral=True
            )
            return

        # Recompute target date at click-time (in case the message is stale)
        offset = self._offset
        target = _today_local_date() + timedelta(days=offset)

        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            options, court_names, grid_text = await asyncio.to_thread(_collect_day_options, target)
        except Exception as e:
            await interaction.followup.send(f":x: API error: `{type(e).__name__}: {e}`", ephemeral=True)
            return

        if not options:
            await interaction.followup.send(
                f"No available slots on {target:%a %d %b}.\n\n{grid_text}",
                ephemeral=True,
            )
            return

        # Two-step: first pick a time. Build a unique list of times.
        # The court selection comes after.
        unique_times = sorted({o[0] for o in options})
        view = _SelectTimeView(target, options, court_names, unique_times, self.owner_id)
        await interaction.followup.send(
            f"**{target:%a %d %b}** — pick a time, then a court.\n{grid_text}",
            view=view,
            ephemeral=True,
        )


def _collect_day_options(target: datetime) -> tuple[list[tuple[str, str, int, str]], dict[int, str], str]:
    """Returns ((HH:MM, court_name, parent_id, label), ...) sorted by time then court,
    court_names dict, and a rendered ANSI grid string for visual reference."""
    with BackboneClient() as c:
        slots = find_slots_window(c, start_date=target, days=1)
        court_ids = {s.bookable_product_id for s in slots}
        names = get_product_names(c, court_ids)
    tz = _tz()
    options: list[tuple[str, str, int, str]] = []
    for s in slots:
        local = s.start_dt.astimezone(tz)
        court = names.get(s.bookable_product_id, f"Court {s.bookable_product_id}")
        time_str = local.strftime("%H:%M")
        label = f"{time_str}  {court}"
        options.append((time_str, court, s.bookable_product_id, label))
    # Sort by time first, then by court name — keeps all courts at a time grouped.
    options.sort(key=lambda x: (x[0], x[1]))

    # Build a small ANSI grid for visual reference (just this one day).
    by_t: dict[str, set[int]] = {}
    for s in slots:
        local = s.start_dt.astimezone(tz)
        by_t.setdefault(local.strftime("%H:%M"), set()).add(s.bookable_product_id)
    courts_sorted = sorted(court_ids, key=lambda c: names.get(c, str(c)))

    def col_label(cid: int) -> str:
        n = names.get(cid, f"#{cid}")
        if n.lower().startswith("padel"):
            rest = n[5:].strip()
            return f"P{rest}" if rest else "P?"
        return n[:4]

    if by_t:
        rows = []
        for t in sorted(by_t.keys()):
            cells = "  ".join(
                (f"{GREEN}{col_label(c)}{RESET}" if c in by_t[t] else f"{GRAY}{col_label(c)}{RESET}")
                for c in courts_sorted
            )
            rows.append(f"{t}  {cells}")
        grid_text = "```ansi\n" + "\n".join(rows) + "\n```"
    else:
        grid_text = ""

    return options[:25], names, grid_text


class _SelectTimeView(discord.ui.View):
    """Step 1: pick a time. Up to 25 unique times — well within Discord's limit."""
    def __init__(self, target_day: datetime, options, court_names, unique_times, owner_id: int):
        super().__init__(timeout=300)
        self.add_item(_SelectTimeMenu(target_day, options, court_names, unique_times, owner_id))


class _SelectTimeMenu(discord.ui.Select):
    def __init__(self, target_day: datetime, options, court_names, unique_times, owner_id: int):
        # Count courts per time so user sees "11:30 (3 courts)"
        by_time: dict[str, list[tuple[str, int]]] = {}
        for time_str, court, parent_id, _label in options:
            by_time.setdefault(time_str, []).append((court, parent_id))
        opts = []
        for t in unique_times[:25]:
            n_courts = len(by_time[t])
            opts.append(discord.SelectOption(
                label=f"{t}  ({n_courts} court{'s' if n_courts != 1 else ''})",
                value=t,
            ))
        super().__init__(
            placeholder="Pick a time",
            min_values=1, max_values=1,
            options=opts,
        )
        self.target_day = target_day
        self.options_data = options
        self.court_names = court_names
        self.owner_id = owner_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(":no_entry_sign:", ephemeral=True)
            return
        chosen_time = self.values[0]
        # Filter to courts available at that time
        court_opts = [
            (court, parent_id)
            for (time_str, court, parent_id, _l) in self.options_data
            if time_str == chosen_time
        ]
        view = _SelectCourtView(self.target_day, chosen_time, court_opts, self.owner_id)
        await interaction.response.send_message(
            f"**{self.target_day:%a %d %b} {chosen_time}** — pick a court:",
            view=view, ephemeral=True,
        )


class _SelectCourtView(discord.ui.View):
    """Step 2: pick a court at the chosen time."""
    def __init__(self, target_day: datetime, time_str: str, court_opts, owner_id: int):
        super().__init__(timeout=300)
        self.add_item(_SelectCourtMenu(target_day, time_str, court_opts, owner_id))


class _SelectCourtMenu(discord.ui.Select):
    def __init__(self, target_day: datetime, time_str: str, court_opts, owner_id: int):
        opts = [
            discord.SelectOption(label=court[:100], value=f"{parent_id}")
            for (court, parent_id) in court_opts[:25]
        ]
        super().__init__(placeholder=f"Pick a court for {time_str}", options=opts)
        self.target_day = target_day
        self.time_str = time_str
        self.owner_id = owner_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(":no_entry_sign:", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        parent_id = int(self.values[0])
        time_str = self.time_str

        def _book_now() -> tuple[bool, BookingResult | None, str | None]:
            try:
                with BackboneClient() as c:
                    me = c.me()
                    slots = find_slots_window(c, start_date=self.target_day, days=1)
                    names = get_product_names(c, {s.bookable_product_id for s in slots})
                    target_name = names.get(parent_id, "")
                    slot = find_slot_at_time(slots, time_str, court=target_name, court_names=names)
                    if not slot:
                        return False, None, f"Slot {time_str} on {target_name} no longer available."
                    res = do_book(c, slot, member_id=me["id"], me=me)
                return True, res, None
            except Exception as e:
                return False, None, f"{type(e).__name__}: {e}"

        ok, res, err = await asyncio.to_thread(_book_now)
        if ok and res:
            if res.paid:
                paid_line = f":white_check_mark: Paid via **{res.pay_method}**."
            elif res.payment_url:
                # Was a card stored? If yes, the driver ran and failed (show error).
                # If no card stored, prompt user to run set-card for full autonomy.
                from .worldline_driver import load_card as _lc
                if _lc() is None:
                    paid_line = (
                        f":credit_card: **Click to pay** (€{res.amount}, 60-min window):\n"
                        f"{res.payment_url}\n"
                        f"-# Run `padelbot set-card` on the Pi to enable headless auto-pay."
                    )
                else:
                    paid_line = (
                        f":warning: Headless auto-pay FAILED — fall back to clicking:\n"
                        f"{res.payment_url}\n"
                        f"-# Driver error: `{res.pay_error or 'unknown'}` — diagnostics in `data/worldline_diag/`."
                    )
            else:
                paid_line = (
                    f":hourglass: Pay manually in the KU Leuven Sport app within 60 min.\n"
                    f"(Auto-pay said: `{res.pay_error or 'no method available'}`)"
                )
            await interaction.followup.send(
                f":tennis: **Booked!** {res.description}\n"
                f"sale `{res.sale_id}` booking `{res.booking_id}` `{res.amount} EUR`\n"
                f"{paid_line}",
                ephemeral=True,
            )
            # Refresh live messages immediately so the user sees the change.
            client = interaction.client
            for coro_name in ("_refresh_slots_message", "_refresh_bookings_message"):
                fn = getattr(client, coro_name, None)
                if fn:
                    asyncio.create_task(fn())
        else:
            await interaction.followup.send(
                f":x: Booking failed: `{err}`", ephemeral=True
            )
