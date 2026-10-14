"""Legacy live channel embeds and persistent buttons opening the current UI."""
from __future__ import annotations
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord

from .booking import Slot
from .config import settings
from . import weather


GREEN = "\x1b[1;32m"
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
    """One field per day with free courts and the member's booking markers."""
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

    total_slots = 0
    forecast = weather.peek_forecast()
    # Reverse order: latest day at top, today at bottom.
    for offset in reversed(range(num_days)):
        d = today + timedelta(days=offset)
        day_label = d.strftime("%a %d %b")
        time_map = by_day.get(day_label, {})
        is_hol, has_bk = day_meta.get(day_label, (False, False))
        day_key = d.strftime("%Y-%m-%d")
        n_open = sum(len(v) for v in time_map.values())
        total_slots += n_open

        tag = ""
        if is_hol and not has_bk:
            tag = " ⚠️"
        elif is_hol and has_bk:
            tag = " 🎉"
        if day_key in my_booking_days:
            tag += " 🎾 you have a booking this day"

        if not time_map:
            value = "*no slots*"
        else:
            # Plain monospace grid — fits 8 days within Discord's 6000-char cap.
            # X=available, .=not. Header row shows P1..P5.
            header = "      " + " ".join(col_label(c) for c in courts_sorted)
            rows = [header]
            for t in sorted(time_map.keys()):
                cells = " ".join(("X " if c in time_map[t] else ". ") for c in courts_sorted)
                rows.append(f"{t} {cells}".rstrip())
            value = "```\n" + "\n".join(rows) + "\n```"
            if len(value) > 1020:
                value = value[:1015] + "\n```"

        badge = forecast.day_badge(d.date()) if forecast else ""
        embed.add_field(name=f"{day_label} — {n_open}{tag}{' · ' + badge if badge else ''}", value=value, inline=False)

    refresh_min = max(1, settings.padelbot_slots_refresh_s // 60)
    legend = "  ".join(f"{col_label(c)}={court_names.get(c, str(c))}" for c in courts_sorted)
    quota_str = f"quota {my_bookings_count}/{WEEKLY_BOOKING_QUOTA}"
    footer = f"{total_slots} open · {quota_str} · {legend} · refresh every {refresh_min} min · click day to book"
    if member_name:
        footer = f"{member_name} · " + footer
    if forecast:
        footer += " · " + weather.CREDIT
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
    forecast = weather.peek_forecast()
    for b in my_bookings[:25]:
        local = b.start_dt.astimezone(tz)
        court = court_names.get(b.raw.get("productId"), "?")
        paid = "PAID" if b.paid_for else "UNPAID"
        badge = forecast.badge(local) if forecast else ""
        lines.append(
            f"{GREEN}{local:%a %d %b  %H:%M}  {court}{'  ' + badge if badge else ''}{RESET}\n"
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

        from .discord_ui import DaySlotsView
        target = _today_local_date() + timedelta(days=self._offset)
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            view = await DaySlotsView.load(self.owner_id, target)
        except Exception:
            await interaction.followup.send("Beschikbaarheid ophalen mislukt. Probeer opnieuw.", ephemeral=True)
            return
        await interaction.followup.send(embed=view.embed(), view=view, ephemeral=True)
