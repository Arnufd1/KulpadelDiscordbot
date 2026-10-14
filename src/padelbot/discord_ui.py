"""Discord panels for availability, reservations and weekly automation."""
from __future__ import annotations

import asyncio
import io
import re
import time as monotonic_time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from loguru import logger

from .booking import book, find_slots_window, get_product_names, BookingPriceChangedError, _timestamp_key
from .booking_manager import cancel_booking, list_my_bookings
from .client import BackboneClient
from .config import settings
from .checkout import BookingQuote, make_quote, slot_price, automatic_payment_ready
from .payment import payment_message
from .match_ui import ShareBookingView, publish_bookings, cancel_shared_booking, MAX_SHARE_BOOKINGS
from .scheduler import compute_next_fire
from . import weather


WEEKDAYS = ("maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag")
AUTOBOOK_HOURS = tuple(f"{hour:02}:00" for hour in range(24))


def today() -> datetime:
    return datetime.now(ZoneInfo(settings.padel_local_tz)).replace(hour=0, minute=0, second=0, microsecond=0)


def local_time(slot) -> str:
    return slot.start_dt.astimezone(ZoneInfo(settings.padel_local_tz)).strftime("%H:%M")


def parse_day_range(value: str) -> tuple[int, int]:
    """Parse a day offset or inclusive range, allowing spaces around the dash."""
    match = re.fullmatch(r"\s*([0-9]{1,3})\s*(?:[-–]\s*([0-9]{1,3})\s*)?", str(value))
    if match:
        first = int(match[1])
        last = int(match[2]) if match[2] is not None else first
        if 0 <= first <= last <= 365:
            return first, last
    raise ValueError("Gebruik een dag zoals `0`, `1` of `2`, of een bereik zoals `0-5` (inclusief). De dagen moeten tussen 0 en 365 liggen, van klein naar groot.")


def fetch_range(start, end):
    # Slots cannot be booked past this horizon; avoid querying a year of
    # schedules when the user includes future days in the overview.
    query_end = min(end, today() + timedelta(days=settings.padel_opens_days_ahead))
    if start > query_end:
        return [], {}
    with BackboneClient() as client:
        slots = find_slots_window(client, start_date=start, days=(query_end.date() - start.date()).days + 1)
        names = get_product_names(client, {s.bookable_product_id for s in slots})
    return slots, names


def fetch_day(target):
    with BackboneClient() as client:
        slots = find_slots_window(client, start_date=target, days=1)
        names = get_product_names(client, {s.bookable_product_id for s in slots})
    return slots, names


def fetch_bookings():
    with BackboneClient() as client:
        me = client.me()
        mine = list_my_bookings(client, member_id=me["id"], upcoming_only=True)
        names = get_product_names(client, {b.raw["productId"] for b in mine if b.raw.get("productId")})
    return mine, names


async def refresh_live(client):
    for name in ("_refresh_slots_message", "_refresh_bookings_message"):
        fn = getattr(client, name, None)
        if fn:
            asyncio.create_task(fn())


class OwnerView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.busy = False

    async def interaction_check(self, interaction):
        if not self.owner_id or interaction.user.id != self.owner_id:
            await interaction.response.send_message("Alleen de eigenaar kan deze knoppen gebruiken.", ephemeral=True)
            return False
        if self.busy:
            await interaction.response.send_message("Deze actie is al verwerkt. Vernieuw het overzicht.", ephemeral=True)
            return False
        return True

    def button(self, label, callback, *, row=3, style=discord.ButtonStyle.secondary, disabled=False):
        item = discord.ui.Button(label=label, row=row, style=style, disabled=disabled)
        item.callback = callback
        self.add_item(item)


class AvailabilityOverview:
    """A dated snapshot shared by private and public pagination controls."""
    def __init__(self, start, end, slots, names, forecast=None):
        self.start, self.end, self.slots, self.names, self.forecast = start, end, slots, names, forecast
        self.checked_at = datetime.now(ZoneInfo(settings.padel_local_tz))
        self.share_attempted, self.shared_url = False, None
        self.by_day = {}
        for slot in slots:
            day = slot.start_dt.astimezone(ZoneInfo(settings.padel_local_tz)).date()
            self.by_day.setdefault(day, []).append(slot)
        self.pages = []
        body, days = "", []
        target = start
        opens_until = today() + timedelta(days=settings.padel_opens_days_ahead)
        while target.date() <= end.date():
            heading = f"**{WEEKDAYS[target.weekday()]} {target:%d/%m/%Y}**"
            if forecast and (badge := forecast.day_badge(target.date())):
                heading += f" · {badge}"
            grouped = {}
            samples = {}
            for slot in self.by_day.get(target.date(), []):
                clock = local_time(slot)
                grouped.setdefault(clock, set()).add(discord.utils.escape_markdown(names.get(slot.bookable_product_id, f"Veld {slot.bookable_product_id}")))
                samples[clock] = slot
            lines = []
            for clock, courts in sorted(grouped.items()):
                badge = forecast.badge(samples[clock].start_dt) if forecast else None
                lines.append(f"**{clock}**{' · ' + badge if badge else ''} · {', '.join(sorted(courts))}")
            if not lines:
                lines = ["Nog niet boekbaar: KUL opent reservaties maximaal 7 dagen vooraf."
                         if target > opens_until else "Geen beschikbare padelvelden."]
            # Keep every line, including empty days. Repeat the date when a
            # long day spills onto the next Discord embed.
            for line in lines:
                for offset in range(0, len(line), 3500):
                    piece = line[offset:offset + 3500]
                    prefix = "" if target in days else ("\n\n" if body else "") + heading + "\n"
                    addition = prefix + piece + "\n"
                    if len(body) + len(addition) > 3900 or (target not in days and len(days) == 25):
                        self.pages.append((body.rstrip(), tuple(days)))
                        body, days = "", []
                        addition = heading + "\n" + piece + "\n"
                    body += addition
                    if target not in days:
                        days.append(target)
            target += timedelta(days=1)
        if body:
            self.pages.append((body.rstrip(), tuple(days)))

    def embed(self, page=0):
        dates = f"{self.start:%d/%m/%Y}" if self.start == self.end else f"{self.start:%d/%m/%Y} t/m {self.end:%d/%m/%Y}"
        embed = discord.Embed(title=f"Vrije padelvelden · {dates}", description=self.pages[page][0], color=0x377DFF, timestamp=self.checked_at)
        embed.set_footer(text=f"Pagina {page + 1}/{len(self.pages)} · momentopname · beschikbaarheid wordt bij boeken opnieuw gecontroleerd"
                         + (" · " + weather.CREDIT if self.forecast else ""))
        return embed


class SlotsRangeView(OwnerView):
    def __init__(self, owner_id, overview, *, page=0, public=False):
        super().__init__(owner_id)
        self.overview, self.public = overview, public
        self.page = min(max(0, page), len(overview.pages) - 1)
        self.pages = len(overview.pages)
        if not public:
            select = discord.ui.Select(placeholder="Kies een dag om een tijdstip en veld te boeken", row=0, options=[
                discord.SelectOption(label=f"{WEEKDAYS[day.weekday()]} {day:%d/%m/%Y}", value=day.date().isoformat())
                for day in overview.pages[self.page][1]
            ])
            async def choose_day(interaction):
                target = next(day for day in overview.pages[self.page][1] if day.date().isoformat() == select.values[0])
                view = DaySlotsView(owner_id, target, overview.by_day.get(target.date(), []), overview.names,
                                    forecast=overview.forecast, overview=overview, overview_page=self.page)
                self.stop()
                await interaction.response.edit_message(content=None, embed=view.embed(), view=view)
            select.callback = choose_day
            self.add_item(select)
        if self.pages > 1:
            self.button("Vorige pagina", self.previous_page, row=1, disabled=self.page == 0)
            self.button("Volgende pagina", self.next_page, row=1, disabled=self.page + 1 >= self.pages)
        if not public:
            self.button("Vernieuwen", self.refresh, row=2)
            self.button("Deel met groep", self.share, row=2, style=discord.ButtonStyle.primary)

    @classmethod
    async def load(cls, owner_id, start, end):
        (slots, names), forecast = await asyncio.gather(asyncio.to_thread(fetch_range, start, end), asyncio.to_thread(weather.get_forecast))
        return cls(owner_id, AvailabilityOverview(start, end, slots, names, forecast))

    async def interaction_check(self, interaction):
        return True if self.public else await super().interaction_check(interaction)

    def embed(self):
        return self.overview.embed(self.page)

    async def show_page(self, interaction, page):
        view = SlotsRangeView(self.owner_id, self.overview, page=page, public=self.public)
        self.stop()
        await interaction.response.edit_message(content=None, embed=view.embed(), view=view)

    async def previous_page(self, interaction):
        await self.show_page(interaction, self.page - 1)

    async def next_page(self, interaction):
        await self.show_page(interaction, self.page + 1)

    async def refresh(self, interaction):
        await interaction.response.defer()
        try:
            view = await SlotsRangeView.load(self.owner_id, self.overview.start, self.overview.end)
        except Exception:
            await interaction.followup.send("Beschikbaarheid ophalen mislukt. Probeer opnieuw.", ephemeral=True)
            return
        self.stop()
        await interaction.edit_original_response(content=None, embed=view.embed(), view=view)

    async def share(self, interaction):
        await share_availability(interaction, self.owner_id, self.overview)


async def share_availability(interaction, owner_id, overview):
    if not owner_id or interaction.user.id != owner_id:
        await interaction.response.send_message("Alleen de eigenaar kan dit overzicht delen.", ephemeral=True)
        return
    if overview.share_attempted:
        message = f"Dit overzicht is al gedeeld: {overview.shared_url}" if overview.shared_url else "Delen is al gestart. Controleer de chat; vernieuw het overzicht voor een nieuwe deelactie."
        await interaction.response.send_message(message, ephemeral=True)
        return
    # Set before awaiting, and do not retry an uncertain send automatically.
    overview.share_attempted = True
    await interaction.response.defer(thinking=False)
    view = SlotsRangeView(owner_id, overview, public=True)
    kwargs = {}
    if len(overview.pages) > 1:
        # The complete snapshot stays readable after pagination controls expire.
        text = f"Vrije padelvelden · gecontroleerd {overview.checked_at:%d/%m/%Y %H:%M}\n\n" + "\n\n".join(body for body, _ in overview.pages)
        kwargs["file"] = discord.File(io.BytesIO(text.encode("utf-8")), filename="padel-beschikbaarheid.txt")
    try:
        message = await interaction.followup.send(embed=view.embed(), view=view if view.children else None,
            ephemeral=False, wait=True, allowed_mentions=discord.AllowedMentions.none(), **kwargs)
        if message.flags.ephemeral:
            raise RuntimeError("Discord heeft het overzicht alleen privé getoond.")
        overview.shared_url = message.jump_url
    except Exception as error:
        view.stop()
        logger.warning("Availability sharing failed: {}", type(error).__name__)
        await interaction.followup.send("Delen niet bevestigd. Controleer de chat en of de app daar openbaar mag antwoorden. Vernieuw het overzicht voor een nieuwe deelactie.", ephemeral=True)
    finally:
        if "file" in kwargs:
            kwargs["file"].close()


class DaySlotsView(OwnerView):
    def __init__(self, owner_id, target, slots, names, *, page=0, time=None, court=None, forecast=None, overview=None, overview_page=0):
        super().__init__(owner_id)
        self.forecast = forecast
        self.overview, self.overview_page = overview, overview_page
        self.share_overview = AvailabilityOverview(target, target, slots, names, forecast)
        self.target, self.slots, self.names = target, slots, names
        self.page, self.time, self.court = page, time, court
        times = sorted({local_time(s) for s in slots})
        self.pages = max(1, (len(times) + 24) // 25)
        if times:
            select = discord.ui.Select(placeholder="Kies een tijdstip", row=0, options=[
                discord.SelectOption(label=t, value=t, default=t == time)
                for t in times[page * 25:(page + 1) * 25]
            ])
            async def select_time(interaction):
                view = DaySlotsView(owner_id, target, slots, names, page=page, time=select.values[0], forecast=self.forecast, overview=self.overview, overview_page=self.overview_page)
                view.share_overview = self.share_overview
                await interaction.response.edit_message(embed=view.embed(), view=view)
            select.callback = select_time
            self.add_item(select)
        if time:
            courts = sorted({s.bookable_product_id for s in slots if local_time(s) == time}, key=lambda p: names.get(p, str(p)))
            court_select = discord.ui.Select(placeholder="Kies een veld", row=1, options=[
                discord.SelectOption(label=names.get(p, f"Veld {p}")[:100], value=str(p), default=p == court)
                for p in courts
            ])
            async def select_court(interaction):
                view = DaySlotsView(owner_id, target, slots, names, page=page, time=time, court=int(court_select.values[0]), forecast=self.forecast, overview=self.overview, overview_page=self.overview_page)
                view.share_overview = self.share_overview
                await view.reserve(interaction)
            court_select.callback = select_court
            self.add_item(court_select)
        if self.pages > 1:
            self.button("Eerdere tijden", self.previous_times, row=2, disabled=page == 0)
            self.button("Latere tijden", self.next_times, row=2, disabled=page + 1 >= self.pages)
        self.button("Vorige dag", self.previous_day, disabled=target.date() <= today().date())
        self.button("Volgende dag", self.next_day)
        self.button("Vernieuwen", self.refresh)
        self.button("Deel met groep", self.share, style=discord.ButtonStyle.primary)
        if overview is not None:
            self.button("Terug naar alle dagen", self.back_to_overview, row=4)
        if court is not None:
            self.button("Boeken", self.reserve, row=4, style=discord.ButtonStyle.success)

    @classmethod
    async def load(cls, owner_id, target):
        (slots, names), forecast = await asyncio.gather(asyncio.to_thread(fetch_day, target), asyncio.to_thread(weather.get_forecast))
        return cls(owner_id, target, slots, names, forecast=forecast)

    def embed(self):
        grouped = {}
        for slot in self.slots:
            grouped.setdefault(local_time(slot), set()).add(self.names.get(slot.bookable_product_id, "Veld"))
        badges = {local_time(slot): self.forecast.badge(slot.start_dt) for slot in self.slots} if self.forecast else {}
        lines = [f"**{t}**{' · ' + badges[t] if badges.get(t) else ''} · {', '.join(sorted(courts))}" for t, courts in sorted(grouped.items())]
        description = "\n".join(lines) or "Geen beschikbare padelvelden op deze dag."
        if self.forecast and (badge := self.forecast.day_badge(self.target.date())):
            description = f"Leuven {badge}\n\n" + description
        if self.target > today() + timedelta(days=settings.padel_opens_days_ahead):
            description += "\nKUL opent reservaties maximaal 7 dagen vooraf."
        if self.court is not None:
            description += f"\n\n**Gekozen:** {self.time} · {self.names.get(self.court, 'Veld')}. Klik op **Boeken**."
        embed = discord.Embed(title=f"Padel · {self.target:%d/%m/%Y}", description=description[:4096], color=0x377DFF,
                              timestamp=datetime.now(ZoneInfo(settings.padel_local_tz)))
        embed.set_footer(text=f"{len(self.slots)} vrije slots · beschikbaarheid wordt bij boeken opnieuw gecontroleerd" + (" · " + weather.CREDIT if self.forecast else ""))
        return embed

    async def show_day(self, interaction, target):
        await interaction.response.defer()
        try:
            view = await DaySlotsView.load(self.owner_id, target)
            view.overview, view.overview_page = self.overview, self.overview_page
            if self.overview is not None:
                view.button("Terug naar alle dagen", view.back_to_overview, row=4)
        except Exception:
            await interaction.followup.send("Beschikbaarheid ophalen mislukt. Probeer Vernieuwen.", ephemeral=True)
            return
        await interaction.edit_original_response(embed=view.embed(), view=view)

    async def previous_day(self, interaction):
        await self.show_day(interaction, max(today(), self.target - timedelta(days=1)))

    async def next_day(self, interaction):
        await self.show_day(interaction, self.target + timedelta(days=1))

    async def refresh(self, interaction):
        await self.show_day(interaction, self.target)

    async def share(self, interaction):
        await share_availability(interaction, self.owner_id, self.share_overview)

    async def back_to_overview(self, interaction):
        view = SlotsRangeView(self.owner_id, self.overview, page=self.overview_page)
        self.stop()
        await interaction.response.edit_message(content=None, embed=view.embed(), view=view)

    async def show_times(self, interaction, page):
        view = DaySlotsView(self.owner_id, self.target, self.slots, self.names, page=page, forecast=self.forecast, overview=self.overview, overview_page=self.overview_page)
        view.share_overview = self.share_overview
        await interaction.response.edit_message(embed=view.embed(), view=view)

    async def previous_times(self, interaction):
        await self.show_times(interaction, self.page - 1)

    async def next_times(self, interaction):
        await self.show_times(interaction, self.page + 1)

    def selected_slot(self, client):
        slots = find_slots_window(client, start_date=self.target, days=1)
        slot = next((s for s in slots if s.bookable_product_id == self.court and local_time(s) == self.time), None)
        if slot is None:
            raise RuntimeError("Dit slot is intussen bezet. Vernieuw de beschikbaarheid.")
        return slot

    def check_my_bookings(self, client, me, slot):
        mine = list_my_bookings(client, member_id=me["id"], upcoming_only=True)
        if any(_timestamp_key(b.start_iso) <= _timestamp_key(slot.end_iso)
               and _timestamp_key(b.end_iso) >= _timestamp_key(slot.start_iso) for b in mine):
            raise RuntimeError("Je hebt al een reservatie die dit tijdstip overlapt.")

    def prepare_confirmation(self):
        with BackboneClient() as client:
            me = client.me()
            slot = self.selected_slot(client)
            self.check_my_bookings(client, me, slot)
            return make_quote(client, slot, me)

    def reserve_now(self, quote: BookingQuote, *, on_3ds_challenge=None):
        if monotonic_time.monotonic() > quote.expires_at:
            raise RuntimeError("Deze bevestiging is verlopen. Kies het slot opnieuw.")
        with BackboneClient() as client:
            me = client.me()
            if me["id"] != quote.member_id:
                raise RuntimeError("Het aangemelde account is gewijzigd. Open het overzicht opnieuw.")
            slot = self.selected_slot(client)
            if (slot.bookable_linked_product_id != quote.slot.bookable_linked_product_id
                    or _timestamp_key(slot.start_iso) != _timestamp_key(quote.slot.start_iso)
                    or _timestamp_key(slot.end_iso) != _timestamp_key(quote.slot.end_iso)):
                raise RuntimeError("Het gekozen slot is gewijzigd. Kies opnieuw.")
            self.check_my_bookings(client, me, slot)
            if slot_price(client, slot, me) != quote.amount:
                raise RuntimeError("De prijs is gewijzigd. Kies het slot opnieuw om het nieuwe bedrag te bekijken.")
            automatic_payment_ready(me, quote.amount)
            try:
                return book(client, slot, member_id=me["id"], me=me,
                            expected_amount=quote.amount, on_3ds_challenge=on_3ds_challenge)
            except BookingPriceChangedError as error:
                if error.result.booking_id is not None:
                    try:
                        cancel_booking(client, booking_id=error.result.booking_id, member_id=me["id"])
                    except Exception:
                        raise RuntimeError("KUL wijzigde het bedrag. Er is niets betaald; controleer de reservatie in `/bookings`.") from None
                    raise RuntimeError("KUL wijzigde het bedrag. De nieuwe reservatie is geannuleerd; er is niets betaald.") from None
                raise RuntimeError("KUL wijzigde het bedrag. Er is niets betaald; controleer `/bookings`.") from None

    async def reserve(self, interaction):
        if self.busy:
            return
        self.busy = True
        await interaction.response.defer()
        try:
            quote = await asyncio.to_thread(self.prepare_confirmation)
        except Exception as error:
            self.busy = False
            await interaction.followup.send(str(error)[:1500], ephemeral=True)
            return
        view = ConfirmBookingView(self, quote)
        await interaction.edit_original_response(content=None, embed=view.embed(), view=view)


class ConfirmBookingView(OwnerView):
    """One confirmation authorizes one reservation and its quoted payment."""
    def __init__(self, selection: DaySlotsView, quote: BookingQuote):
        super().__init__(selection.owner_id)
        self.timeout = 120
        self.selection, self.quote = selection, quote
        self.button(f"Bevestigen en betalen · {quote.amount_label}", self.confirm, row=0, style=discord.ButtonStyle.success)
        self.button("Terug", self.back, row=0)

    def embed(self):
        s = self.selection
        local = self.quote.slot.start_dt.astimezone(ZoneInfo(settings.padel_local_tz))
        embed = discord.Embed(title="Boeking bevestigen", color=0x377DFF,
            description=f"**Datum:** {local:%d/%m/%Y}\n**Tijd:** {local:%H:%M}\n"
                        f"**Veld:** {s.names.get(s.court, 'Padelveld')}\n**Totaal:** {self.quote.amount_label}\n\n"
                        "Na bevestiging reserveert en betaalt de bot automatisch. Als je bank een goedkeuring vraagt, bevestig je die in je bank-app.")
        embed.set_footer(text="Deze bevestiging is 2 minuten geldig.")
        if s.forecast and (badge := s.forecast.badge(local)):
            embed.add_field(name="Weer Leuven", value=badge, inline=False)
        return embed

    async def back(self, interaction):
        self.busy = True
        s = self.selection
        view = DaySlotsView(s.owner_id, s.target, s.slots, s.names, page=s.page, time=s.time, court=s.court, forecast=s.forecast, overview=s.overview, overview_page=s.overview_page)
        view.share_overview = s.share_overview
        await interaction.response.edit_message(content=None, embed=view.embed(), view=view)

    async def confirm(self, interaction):
        # Set before the first await: repeated clicks on this panel cannot write twice.
        if self.busy:
            return
        self.busy = True
        await interaction.response.defer()
        loop = asyncio.get_running_loop()
        def bank_verification():
            notice = interaction.edit_original_response(
                content="🏦 De bankcontrole loopt. Als je bank-app een goedkeuring vraagt, bevestig die. De bot wacht op het resultaat.",
                embed=None, view=None,
            )
            try:
                asyncio.run_coroutine_threadsafe(notice, loop).result(timeout=10)
            except Exception as error:
                logger.warning("Bank-verification notice failed: {}", type(error).__name__)
        try:
            async with interaction.client._booking_lock:
                logger.info("Confirmed booking: parent={} linked={} start={} amount={} EUR",
                            self.quote.slot.bookable_product_id, self.quote.slot.bookable_linked_product_id,
                            self.quote.slot.start_iso, self.quote.amount)
                await interaction.edit_original_response(content="⏳ De bot reserveert en betaalt. Je hoeft de betaallink niet te openen.", embed=None, view=None)
                result = await asyncio.to_thread(self.selection.reserve_now, self.quote, on_3ds_challenge=bank_verification)
            s = self.selection
            heading = "Gereserveerd en betaald" if result.paid else "Reservatie aangemaakt; betaling nog niet bevestigd"
            message = f"🎾 **{heading}:** {s.target:%d/%m} {s.time} · {s.names.get(s.court, 'Veld')}\n{payment_message(result)}"
            shared_view = ShareBookingView(self.owner_id, result.booking_id) if result.paid and getattr(result, "booking_id", None) else None
        except Exception as error:
            message = f"Boeking niet bevestigd: {error}\nControleer `/bookings` voordat je opnieuw boekt."
            shared_view = None
        await interaction.edit_original_response(content=message[:2000], embed=None, view=shared_view)
        await refresh_live(interaction.client)


class BookingsView(OwnerView):
    def __init__(self, owner_id, mine, names, *, page=0, selected=None, forecast=None):
        super().__init__(owner_id)
        self.forecast = forecast
        self.mine, self.names, self.page = mine, names, page
        requested = set(() if selected is None else (selected,) if isinstance(selected, int) else selected)
        self.selected_ids = tuple(b.id for b in mine if b.id in requested)
        if len(self.selected_ids) > MAX_SHARE_BOOKINGS:
            raise ValueError(f"Selecteer maximaal {MAX_SHARE_BOOKINGS} reservaties.")
        self.pages = max(1, (len(mine) + 24) // 25)
        page_bookings = mine[page * 25:(page + 1) * 25]
        if page_bookings:
            page_ids = {b.id for b in page_bookings}
            retained = tuple(bid for bid in self.selected_ids if bid not in page_ids)
            remaining = MAX_SHARE_BOOKINGS - len(retained)
            select = discord.ui.Select(placeholder="Kies één of meerdere reservaties", row=0,
                min_values=0, max_values=max(1, min(len(page_bookings), remaining)), disabled=remaining == 0, options=[
                discord.SelectOption(label=self.label(b)[:100], value=str(b.id), default=b.id in self.selected_ids)
                for b in page_bookings
            ])
            async def choose(interaction):
                chosen = (*retained, *(int(value) for value in select.values))
                view = BookingsView(owner_id, mine, names, page=page, selected=chosen, forecast=self.forecast)
                self.stop()
                await interaction.response.edit_message(content=None, embed=view.embed(), view=view)
            select.callback = choose
            self.add_item(select)
        if self.pages > 1:
            self.button("Vorige", self.previous_page, disabled=page == 0)
            self.button("Volgende", self.next_page, disabled=page + 1 >= self.pages)
        self.button("Vernieuwen", self.refresh)
        if self.selected_ids:
            self.button("Selectie wissen", self.clear_selection)
            count = len(self.selected_ids)
            self.button("Deel met groep" if count == 1 else f"Deel selectie ({count})", self.share, row=2, style=discord.ButtonStyle.primary)
            self.button("Deel + @everyone" if count == 1 else "Deel selectie + @everyone", self.share_everyone, row=2)
        if self.selected is not None:
            self.button("Annuleer deze reservatie", self.cancel, row=2, style=discord.ButtonStyle.danger)

    @property
    def selected(self):
        return self.selected_ids[0] if len(self.selected_ids) == 1 else None

    @classmethod
    async def load(cls, owner_id):
        (mine, names), forecast = await asyncio.gather(asyncio.to_thread(fetch_bookings), asyncio.to_thread(weather.get_forecast))
        return cls(owner_id, mine, names, forecast=forecast)

    def label(self, booking):
        local = booking.start_dt.astimezone(ZoneInfo(settings.padel_local_tz))
        badge = self.forecast.badge(local) if self.forecast else ""
        return f"{local:%d/%m %H:%M} · {self.names.get(booking.raw.get('productId'), 'Veld')}{' · ' + badge if badge else ''}"

    def embed(self):
        lines = [self.label(b) for b in self.mine[self.page * 25:(self.page + 1) * 25]]
        body = "\n".join(lines) or "Je hebt geen komende reservaties."
        chosen = [b for b in self.mine if b.id in self.selected_ids]
        if chosen:
            body += f"\n\n**Geselecteerd ({len(chosen)}):**\n" + "\n".join(self.label(b) for b in chosen)
            body += "\nDeel de selectie met de groep."
            if self.selected is not None:
                body += " Of annuleer deze reservatie met de rode knop."
        embed = discord.Embed(title=f"Mijn reservaties ({len(self.mine)})", description=body[:4096], color=0x00BB00)
        embed.set_footer(text=f"Pagina {self.page + 1}/{self.pages} · maximaal {MAX_SHARE_BOOKINGS} reservaties per deelactie" + (" · " + weather.CREDIT if self.forecast else ""))
        return embed

    async def refresh(self, interaction):
        await interaction.response.defer()
        try:
            view = await BookingsView.load(self.owner_id)
        except Exception:
            await interaction.followup.send("Reservaties ophalen mislukt. Probeer opnieuw.", ephemeral=True)
            return
        self.stop()
        await interaction.edit_original_response(content=None, embed=view.embed(), view=view)

    async def show_page(self, interaction, page):
        view = BookingsView(self.owner_id, self.mine, self.names, page=page, selected=self.selected_ids, forecast=self.forecast)
        self.stop()
        await interaction.response.edit_message(content=None, embed=view.embed(), view=view)

    async def previous_page(self, interaction):
        await self.show_page(interaction, self.page - 1)

    async def next_page(self, interaction):
        await self.show_page(interaction, self.page + 1)

    async def share(self, interaction):
        if await publish_bookings(interaction, self.owner_id, self.selected_ids) is not None:
            self.stop()

    async def share_everyone(self, interaction):
        if await publish_bookings(interaction, self.owner_id, self.selected_ids, tag_everyone=True) is not None:
            self.stop()

    async def clear_selection(self, interaction):
        view = BookingsView(self.owner_id, self.mine, self.names, page=self.page, forecast=self.forecast)
        self.stop()
        await interaction.response.edit_message(content=None, embed=view.embed(), view=view)

    async def cancel(self, interaction):
        if self.selected is None:
            await interaction.response.send_message("Selecteer één reservatie om te annuleren.", ephemeral=True)
            return
        if self.busy:
            return
        self.busy = True
        await interaction.response.defer()
        def attempt():
            with BackboneClient() as client:
                me = client.me()
                return cancel_booking(client, booking_id=self.selected, member_id=me["id"])
        try:
            async with interaction.client._booking_lock:
                await asyncio.to_thread(attempt)
        except Exception as error:
            self.stop()
            await interaction.edit_original_response(content=f"Annulering niet bevestigd: {error}\nVernieuw `/bookings` om de huidige status te controleren."[:2000], embed=None, view=None)
            return
        # Editing with a new view does not remove the SDK's previous callbacks.
        # Retire the processed view so late clicks use the safe reload fallback.
        self.stop()
        try:
            await cancel_shared_booking(interaction, self.owner_id, self.selected)
        except Exception as error:
            logger.warning("Shared booking cancellation sync failed: {}", type(error).__name__)
        try:
            view = await BookingsView.load(self.owner_id)
            await interaction.edit_original_response(content="✅ Reservatie geannuleerd.", embed=view.embed(), view=view)
        except Exception:
            await interaction.edit_original_response(content="✅ Reservatie geannuleerd. Open `/bookings` voor je bijgewerkte overzicht.", embed=None, view=None)
        await refresh_live(interaction.client)


class AutoBookView(OwnerView):
    def __init__(self, owner_id, store, *, page=0, selected=None):
        super().__init__(owner_id)
        self.store, self.page, self.selected = store, page, selected
        self.rules = store.list_rules()
        self.pages = max(1, (len(self.rules) + 24) // 25)
        self.page = min(page, self.pages - 1)
        batch = self.rules[self.page * 25:(self.page + 1) * 25]
        if batch:
            select = discord.ui.Select(placeholder="Kies een automatische boeking", row=0, options=[
                discord.SelectOption(label=f"{WEEKDAYS[r.day_of_week]} {r.time_label()} · {'actief' if r.enabled else 'gepauzeerd'}", value=str(r.id), default=r.id == selected)
                for r in batch
            ])
            async def choose(interaction):
                view = AutoBookView(owner_id, store, page=self.page, selected=int(select.values[0]))
                await interaction.response.edit_message(embed=view.embed(), view=view)
            select.callback = choose
            self.add_item(select)
        rule = store.get_rule(selected) if selected is not None else None
        if rule:
            self.button("Pauzeren" if rule.enabled else "Hervatten", self.toggle, row=1)
            self.button("Verwijderen", self.remove, row=1, style=discord.ButtonStyle.danger)
        self.button("Nieuwe automatische boeking", self.add, row=2, style=discord.ButtonStyle.success)
        self.button("Vernieuwen", self.refresh, row=2)
        if self.pages > 1:
            self.button("Vorige", self.previous_page, disabled=self.page == 0)
            self.button("Volgende", self.next_page, disabled=self.page + 1 >= self.pages)

    def embed(self):
        lines = []
        for rule in self.rules[self.page * 25:(self.page + 1) * 25]:
            text = f"**{WEEKDAYS[rule.day_of_week]} {rule.time_label()}** · {'actief' if rule.enabled else 'gepauzeerd'}"
            if rule.enabled:
                spec = compute_next_fire(rule)
                text += f"\nVolgende geplande poging: {spec.fire_at:%d/%m %H:%M}"
            lines.append(text)
        description = "Eén boeking per regel per week, op de vroegste vrije starttijd binnen je uurinterval (inclusief einduur). De bot controleert ook of er later een slot vrijkomt.\n\n"
        description += "\n\n".join(lines) or "Nog geen automatische boekingen."
        embed = discord.Embed(title="Automatisch boeken", description=description[:4096], color=0x377DFF)
        embed.set_footer(text=f"Pagina {self.page + 1}/{self.pages} · regels blijven actief tot je ze pauzeert of verwijdert")
        return embed

    async def show(self, interaction, *, page=None, selected=None):
        view = AutoBookView(self.owner_id, self.store, page=self.page if page is None else page, selected=selected)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    async def refresh(self, interaction):
        await self.show(interaction)

    async def previous_page(self, interaction):
        await self.show(interaction, page=self.page - 1)

    async def next_page(self, interaction):
        await self.show(interaction, page=self.page + 1)

    async def add(self, interaction):
        await interaction.response.send_modal(AutoBookModal(self.owner_id, self.store))

    async def toggle(self, interaction):
        rule = self.store.get_rule(self.selected)
        if rule:
            self.store.set_enabled(rule.id, not rule.enabled)
        await self.show(interaction, selected=self.selected)

    async def remove(self, interaction):
        self.store.remove_rule(self.selected)
        await self.show(interaction)


class AutoBookModal(discord.ui.Modal, title="Nieuwe wekelijkse boeking"):
    def __init__(self, owner_id, store):
        super().__init__(timeout=900)
        self.owner_id, self.store = owner_id, store
        self.saved = False
        self.weekday = discord.ui.Select(placeholder="Kies een weekdag", options=[
            discord.SelectOption(label=day.capitalize(), value=day) for day in WEEKDAYS
        ])
        self.start_time = discord.ui.Select(placeholder="Kies het beginuur", options=[
            discord.SelectOption(label=stamp, value=stamp) for stamp in AUTOBOOK_HOURS
        ])
        self.end_time = discord.ui.Select(placeholder="Kies het einduur", options=[
            discord.SelectOption(label=stamp, value=stamp) for stamp in AUTOBOOK_HOURS
        ])
        self.notes = discord.ui.TextInput(required=False, max_length=200)
        self.add_item(discord.ui.Label(text="Weekdag", component=self.weekday))
        self.add_item(discord.ui.Label(text="Beginuur (Belgische tijd)", component=self.start_time,
                                      description="De bot kiest de vroegste vrije starttijd binnen het interval."))
        self.add_item(discord.ui.Label(text="Einduur (Belgische tijd)", component=self.end_time,
                                      description="Inclusief dit startuur. Eén boeking per week; gelijke uren = één vast tijdstip."))
        self.add_item(discord.ui.Label(text="Notitie (optioneel)", component=self.notes))

    async def on_submit(self, interaction):
        if not self.owner_id or interaction.user.id != self.owner_id:
            await interaction.response.send_message("Alleen de eigenaar kan automatisch boeken instellen.", ephemeral=True)
            return
        if self.saved:
            await interaction.response.send_message("Deze regel is al opgeslagen.", ephemeral=True)
            return
        if len(self.weekday.values) != 1 or self.weekday.values[0] not in WEEKDAYS:
            await interaction.response.send_message("Kies een weekdag uit het menu.", ephemeral=True)
            return
        if any(len(select.values) != 1 or select.values[0] not in AUTOBOOK_HOURS
               for select in (self.start_time, self.end_time)):
            await interaction.response.send_message("Kies een begin- en einduur uit de menu's.", ephemeral=True)
            return
        start, end = self.start_time.values[0], self.end_time.values[0]
        if end < start:
            await interaction.response.send_message("Het einduur moet gelijk aan of later dan het beginuur zijn.", ephemeral=True)
            return
        weekday = WEEKDAYS.index(self.weekday.values[0])
        if any(r.day_of_week == weekday and r.time_local == start and (r.end_time_local or r.time_local) == end
               for r in self.store.list_rules()):
            await interaction.response.send_message("Er bestaat al een regel op deze dag met dit uurinterval. Gebruik Hervatten als hij gepauzeerd is.", ephemeral=True)
            return
        self.store.add_rule(weekday, start, str(self.notes).strip() or None, end_time_local=end)
        self.saved = True
        view = AutoBookView(self.owner_id, self.store)
        await interaction.response.edit_message(content="✅ Wekelijkse automatische boeking ingesteld.", embed=view.embed(), view=view)
