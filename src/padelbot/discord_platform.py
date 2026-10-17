"""Thin Discord views over the same tenant queue and booking code as the website."""
import asyncio,time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import discord

from .config import settings
from .booking import Slot
from .discord_ui import AvailabilityOverview, WEEKDAYS, local_time, share_availability, today
from . import weather
from .discord_sharing import SharePolicyError, prepare_public_share, with_server_settings


def label(slot):
    start=datetime.fromisoformat(slot['start'].replace('Z','+00:00')).astimezone(ZoneInfo('Europe/Brussels'))
    return f"{start:%d/%m %H:%M} · {slot.get('name','Padel')}"


async def job(service,user,kind,payload):
    ident=await asyncio.to_thread(service.enqueue,user,kind,payload)
    deadline=time.monotonic()+700
    while time.monotonic()<deadline:
        result=await asyncio.to_thread(service.store.job,user['id'],ident)
        if result['status']=='done':return result['result']
        if result['status'] in {'failed','unknown','cancelled'}:
            raise ValueError(result['result'].get('error','Controleer je boekingen voordat je opnieuw probeert.'))
        await asyncio.sleep(.5)
    raise ValueError('De taak loopt nog. Controleer je dashboard voordat je opnieuw boekt.')


class ProfileView(discord.ui.View):
    def __init__(self,service,user,discord_id,*,timeout=300):
        super().__init__(timeout=timeout);self.service=service;self.user=user;self.discord_id=discord_id;self.busy=False
    async def interaction_check(self,interaction):
        if interaction.user.id!=self.discord_id or self.busy:
            await interaction.response.send_message('Open je eigen overzicht met /checkbookings.',ephemeral=True);return False
        try:
            from .identity import remember_interaction
            remember_interaction(self.service.hub,interaction)
            current=self.service.discord_user(interaction.user.id)
            if current['id']!=self.user['id']:
                raise ValueError('Je profielkoppeling is gewijzigd. Open je eigen overzicht opnieuw met /checkbookings.')
            self.user=current
        except ValueError as error:
            await interaction.response.send_message(str(error),ephemeral=True);return False
        return True
    def button(self,text,callback,**kwargs):
        b=discord.ui.Button(label=text,**kwargs);b.callback=callback;self.add_item(b)


def availability_overview(slots, start, end, forecast=None):
    """Adapt the canonical worker result for the existing Discord formatter."""
    names = {int(slot['product']): slot.get('name', 'Padel') for slot in slots}
    domain_slots = [Slot(int(slot['product']), int(slot['court']), slot['start'],
                         slot['end'], slot.get('price'), slot) for slot in slots]
    return AvailabilityOverview(start, end, domain_slots, names, forecast)


async def load_availability(service, user, start, end):
    # Retain the requested calendar dates, including days with no free courts.
    base = today()
    first, last = (start.date() - base.date()).days, (end.date() - base.date()).days
    horizon = min(30, settings.padel_opens_days_ahead)
    if first > horizon:
        return availability_overview([], start, end, weather.peek_forecast())
    result, forecast = await asyncio.gather(
        job(service, user, 'slots', {'start': max(0, first), 'end': max(0, min(last, horizon))}),
        asyncio.to_thread(weather.get_forecast))
    return availability_overview(result['slots'], start, end, forecast)


def day_overview(overview, target):
    # Selection and pagination must retain the same explicit-share latch.
    snapshots = getattr(overview, '_discord_day_snapshots', None)
    if snapshots is None:
        snapshots = overview._discord_day_snapshots = {}
    if target.date() not in snapshots:
        snapshot = AvailabilityOverview(target, target,
            overview.by_day.get(target.date(), []), overview.names, overview.forecast)
        snapshot.checked_at = overview.checked_at
        snapshots[target.date()] = snapshot
    return snapshots[target.date()]


def availability_dashboard_url(overview):
    """Carry the selected range into the dashboard's shared seven-day cache."""
    base = today().date()
    first = (overview.start.date() - base).days
    last = (overview.end.date() - base).days
    if not 0 <= first <= last <= 6:
        first, last = 0, 6
    return settings.padelbot_public_url.rstrip('/') + f'/#slots/{first}-{last}'


class AvailabilityView(ProfileView):
    async def share(self, interaction):
        # Public snapshots contain availability only; publishing is still a
        # profile action and must respect current bans/restrictions.
        try:
            current = self.service.discord_user(interaction.user.id)
            if interaction.user.id != self.discord_id or current['id'] != self.user['id']:
                raise ValueError('Open je eigen overzicht met /checkbookings.')
            self.service.require_writable(current['id'])
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await share_availability(interaction, self.discord_id, self.share_overview)

    async def refreshed(self, interaction, start, end, make_view):
        if self.busy:
            return
        self.busy = True
        await interaction.response.defer()
        try:
            overview = await load_availability(self.service, self.user, start, end)
            view = make_view(overview)
            await interaction.edit_original_response(content=None, embed=view.embed(), view=with_server_settings(view, interaction))
            self.stop()
        except Exception:
            self.busy = False
            await interaction.followup.send('Beschikbaarheid ophalen mislukt. Probeer Vernieuwen.', ephemeral=True)


class SlotsView(AvailabilityView):
    """The complete dated overview, not a hidden list of individual slots."""
    def __init__(self, service, user, discord_id, overview, *, page=0):
        super().__init__(service, user, discord_id)
        self.overview = self.share_overview = overview
        self.page = min(max(0, page), len(overview.pages) - 1)
        self.select = discord.ui.Select(
            placeholder='Kies een dag om te boeken', row=0,
            options=[discord.SelectOption(label=f'{WEEKDAYS[day.weekday()]} {day:%d/%m/%Y}',
                       value=day.date().isoformat()) for day in overview.pages[self.page][1]])
        self.select.callback = self.choose
        self.add_item(self.select)
        if len(overview.pages) > 1:
            self.button('Vorige pagina', self.previous, row=1, disabled=self.page == 0)
            self.button('Volgende pagina', self.next, row=1, disabled=self.page + 1 == len(overview.pages))
        self.button('Vernieuwen', self.refresh, row=2)
        self.button('Deel met groep', self.share, row=2, style=discord.ButtonStyle.primary)
        self.add_item(discord.ui.Button(label='Dashboard', url=availability_dashboard_url(overview), row=3))

    def embed(self):
        return self.overview.embed(self.page)

    async def show_page(self, interaction, page):
        view = SlotsView(self.service, self.user, self.discord_id, self.overview, page=page)
        await interaction.response.edit_message(content=None, embed=view.embed(), view=with_server_settings(view, interaction))
        self.stop()

    async def previous(self, interaction):
        await self.show_page(interaction, self.page - 1)

    async def next(self, interaction):
        await self.show_page(interaction, self.page + 1)

    async def choose(self, interaction):
        target = next(day for day in self.overview.pages[self.page][1]
                      if day.date().isoformat() == self.select.values[0])
        view = DaySlotsView(self.service, self.user, self.discord_id, self.overview, target,
                            parent=self.overview, parent_page=self.page)
        await interaction.response.edit_message(content=None, embed=view.embed(), view=with_server_settings(view, interaction))
        self.stop()

    async def refresh(self, interaction):
        await self.refreshed(interaction, self.overview.start, self.overview.end,
            lambda overview: SlotsView(self.service, self.user, self.discord_id, overview, page=self.page))


class DaySlotsView(AvailabilityView):
    def __init__(self, service, user, discord_id, overview, target, *, page=0,
                 selected_time=None, parent=None, parent_page=0):
        super().__init__(service, user, discord_id)
        self.overview, self.target = overview, target
        self.parent, self.parent_page = parent, parent_page
        self.share_overview = day_overview(overview, target)
        self.slots = overview.by_day.get(target.date(), [])
        self.selected_time = selected_time
        times = sorted({local_time(slot) for slot in self.slots})
        self.pages = max(1, (len(times) + 24) // 25, len(self.share_overview.pages))
        self.page = min(max(0, page), self.pages - 1)
        if times[self.page * 25:(self.page + 1) * 25]:
            self.time_select = discord.ui.Select(placeholder='Kies een tijdstip', row=0, options=[
                discord.SelectOption(label=clock, value=clock, default=clock == selected_time)
                for clock in times[self.page * 25:(self.page + 1) * 25]])
            self.time_select.callback = self.choose_time
            self.add_item(self.time_select)
        if selected_time:
            self.court_select = discord.ui.Select(placeholder='Kies een veld', row=1, options=[
                discord.SelectOption(label=overview.names.get(slot.bookable_product_id, 'Padel')[:100], value=str(index))
                for index, slot in enumerate(self.slots) if local_time(slot) == selected_time])
            self.court_select.callback = self.choose_court
            self.add_item(self.court_select)
        if self.pages > 1:
            self.button('Eerdere tijden', self.previous_times, row=2, disabled=self.page == 0)
            self.button('Latere tijden', self.next_times, row=2, disabled=self.page + 1 == self.pages)
        self.button('Vorige dag', self.previous_day, row=3, disabled=target.date() <= today().date())
        self.button('Volgende dag', self.next_day, row=3, disabled=target.date() >= (today() + timedelta(days=365)).date())
        self.button('Vernieuwen', self.refresh, row=3)
        self.button('Deel met groep', self.share, row=3, style=discord.ButtonStyle.primary)
        if parent is not None:
            self.button('Terug naar alle dagen', self.back, row=4)
        self.add_item(discord.ui.Button(label='Dashboard', url=availability_dashboard_url(parent or overview), row=4))

    def embed(self):
        return self.share_overview.embed(min(self.page, len(self.share_overview.pages) - 1))

    def replace(self, **kwargs):
        options = {'page': self.page, 'selected_time': self.selected_time,
                   'parent': self.parent, 'parent_page': self.parent_page, **kwargs}
        return DaySlotsView(self.service, self.user, self.discord_id, self.overview, self.target, **options)

    async def show(self, interaction, view):
        await interaction.response.edit_message(content=None, embed=view.embed(), view=with_server_settings(view, interaction))
        self.stop()

    async def choose_time(self, interaction):
        await self.show(interaction, self.replace(selected_time=self.time_select.values[0]))

    async def previous_times(self, interaction):
        await self.show(interaction, self.replace(page=self.page - 1, selected_time=None))

    async def next_times(self, interaction):
        await self.show(interaction, self.replace(page=self.page + 1, selected_time=None))

    async def show_day(self, interaction, target):
        await self.refreshed(interaction, target, target,
            lambda overview: DaySlotsView(self.service, self.user, self.discord_id, overview, target,
                                         parent=self.parent, parent_page=self.parent_page))

    async def previous_day(self, interaction):
        await self.show_day(interaction, max(today(), self.target - timedelta(days=1)))

    async def next_day(self, interaction):
        await self.show_day(interaction, self.target + timedelta(days=1))

    async def refresh(self, interaction):
        await self.show_day(interaction, self.target)

    async def back(self, interaction):
        await self.show(interaction, SlotsView(self.service, self.user, self.discord_id,
                                               self.parent, page=self.parent_page))

    async def choose_court(self, interaction):
        if self.busy:
            return
        self.busy = True
        await interaction.response.defer()
        slot = self.slots[int(self.court_select.values[0])].raw
        try:
            quote = await job(self.service, self.user, 'quote', {
                'target': slot['start'], 'product': slot['product'], 'court': slot['court'], 'amount': 1})
            view = ConfirmView(self.service, self.user, self.discord_id, slot, quote, selection=self)
            mode = self.service.store.preferences(self.user['id']).get('payment_mode', 'kul')
            content = f"**{label(slot)} · €{quote['amount']}**\n" + (
                'Betaallink hier, op je dashboard en in Discord. Voldoende KUL-saldo gebruiken we eerst.'
                if mode == 'discord' else 'Betalen met je standaardkaart. Voldoende KUL-saldo gebruiken we eerst.')
            await interaction.edit_original_response(content=content, embed=None, view=with_server_settings(view, interaction))
            self.stop()
        except Exception as error:
            self.busy = False
            await interaction.followup.send(str(error)[:1900], ephemeral=True)


class ConfirmView(ProfileView):
    def __init__(self,service,user,discord_id,slot,quote,*,selection=None):
        super().__init__(service,user,discord_id,timeout=120);self.slot=slot;self.quote=quote;self.expires=time.monotonic()+120
        self.selection=selection
        self.button('Bevestigen · €'+str(quote['amount']),self.confirm,style=discord.ButtonStyle.success)
        if selection is not None:self.button('Terug',self.back)
        self.button('Sluiten',self.close)
    async def back(self,interaction):
        if self.busy:return
        self.busy=True
        view=self.selection.replace()
        await interaction.response.edit_message(content=None,embed=view.embed(),view=with_server_settings(view,interaction))
        self.stop()
    async def close(self,interaction):self.busy=True;await interaction.response.edit_message(content='Gesloten.',view=None)
    async def confirm(self,interaction):
        if self.busy:return
        self.busy=True;await interaction.response.defer()
        if time.monotonic()>self.expires:
            await interaction.edit_original_response(content='Bevestiging verlopen. Kies het moment opnieuw.',view=None);return
        try:
            result=await job(self.service,self.user,'book',{'target':self.slot['start'],'product':self.slot['product'],'court':self.slot['court'],'amount':self.quote['amount']})
            content=result['message']
            view=BookingResultView(self.service,self.user,self.discord_id,result)
        except Exception as error:content=str(error);view=None
        await interaction.edit_original_response(content=content,view=with_server_settings(view,interaction))


class BookingResultView(ProfileView):
    def __init__(self,service,user,discord_id,result):
        super().__init__(service,user,discord_id)
        self.booking_id=result.get('booking_id')
        if result.get('payment_available') and result.get('sale_id'):
            for payment in service.private_payments(user['id']):
                if payment['sale_id']==result['sale_id']:
                    self.add_item(discord.ui.Button(label='Betalen',url=payment['url']))
                    break
        if result.get('paid') is True and self.booking_id:
            self.button('Deel in deze chat',self.share,style=discord.ButtonStyle.primary)
        self.add_item(discord.ui.Button(label='Dashboard',url=settings.padelbot_public_url+'/#bookings'))
    async def share(self,interaction):
        self.busy=True
        try:await share_booking(interaction,self.service,self.user,self.booking_id)
        finally:self.busy=False


class BookingsView(ProfileView):
    """The selection stays private; an explicit button publishes in this chat."""
    def __init__(self,service,user,discord_id,bookings,*,page=0):
        super().__init__(service,user,discord_id)
        self.bookings=[booking for booking in bookings if booking.get('confirmed') is True]
        self.page=page;self.booking_id=None
        chunk=self.bookings[page*25:(page+1)*25]
        if chunk:
            self.select=discord.ui.Select(placeholder='Kies een boeking om te delen',options=[
                discord.SelectOption(label=label(booking)[:100],value=str(booking['id'])) for booking in chunk])
            self.select.callback=self.choose;self.add_item(self.select)
            self.share_button=discord.ui.Button(label='Deel in deze chat',style=discord.ButtonStyle.primary,disabled=True,row=1)
            self.share_button.callback=self.share;self.add_item(self.share_button)
        if page:self.button('Vorige',self.previous,row=1)
        if (page+1)*25<len(self.bookings):self.button('Volgende',self.next,row=1)
        self.add_item(discord.ui.Button(label='Dashboard',url=settings.padelbot_public_url+'/#bookings',row=2))
    async def choose(self,interaction):
        self.booking_id=int(self.select.values[0]);self.share_button.disabled=False
        for option in self.select.options:option.default=int(option.value)==self.booking_id
        await interaction.response.edit_message(view=self)
    async def previous(self,interaction):
        await interaction.response.edit_message(view=with_server_settings(BookingsView(self.service,self.user,self.discord_id,self.bookings,page=self.page-1),interaction))
    async def next(self,interaction):
        await interaction.response.edit_message(view=with_server_settings(BookingsView(self.service,self.user,self.discord_id,self.bookings,page=self.page+1),interaction))
    async def share(self,interaction):
        if self.booking_id is None:return
        self.busy=True
        try:await share_booking(interaction,self.service,self.user,self.booking_id)
        finally:self.busy=False


async def refresh_match(service,match):
    """Verify the host's reservation, never the global bot owner's account."""
    host=service.store.user(match.hub_profile_id)
    if not host or host['suspended']:raise ValueError('Deze organisator is niet beschikbaar.')
    await job(service,host,'bookings',{})
    fresh=service.community.matches.get(match.id)
    if (not fresh or not fresh.hub_visible or not fresh.hub_confirmed
            or fresh.status!='open' or fresh.started):
        raise ValueError('Deze boeking is niet meer bevestigd of beschikbaar.')
    return fresh


async def share_booking(interaction,service,user,booking_id):
    """Share only a freshly verified booking owned by this linked identity."""
    from .match_ui import send_post
    current=service.discord_user(interaction.user.id)
    if current['id']!=user['id']:
        raise ValueError('Open je eigen boekingen met /bookings.')
    if getattr(getattr(interaction,'context',None),'dm_channel',False):
        await interaction.response.send_message('Gebruik /deel in de groepschat of het serverkanaal.',ephemeral=True)
        return
    try:
        interaction=await prepare_public_share(interaction,'matches')
    except ValueError as error:
        await interaction.response.send_message(str(error),ephemeral=True)
        return
    await interaction.response.defer(thinking=False)
    post=None;send_attempted=False
    try:
        await job(service,current,'bookings',{})
        interaction=await prepare_public_share(interaction,'matches')
        # Community.share binds the profile, recent paid KUL snapshot and roster.
        service.community.share(current['id'],int(booking_id),True)
        match=service.community.matches.for_booking(interaction.user.id,int(booking_id))
        if not match or match.hub_profile_id!=current['id']:
            raise ValueError('Deze boeking hoort niet bij je profiel.')
        bot=interaction.client
        async with bot._match_publish_lock,bot._match_action_lock:
            post,created=service.community.matches.reserve_post(match.id,interaction.user.id,interaction.channel_id,interaction.guild_id)
            if created:
                send_attempted=True
                post=await send_post(interaction,match,post)
            elif post.message_id is None:
                raise ValueError('Delen nog niet bevestigd. Controleer deze chat en klik op Vernieuwen op het groepsbericht.')
        message='Gedeeld.' if created else 'Al gedeeld in deze chat.'
        if post.jump_url:message+=' '+post.jump_url
        await interaction.edit_original_response(content=message,view=None,allowed_mentions=discord.AllowedMentions.none())
    except SharePolicyError as error:
        if post is not None:service.community.matches.drop_pending_post(post.id)
        await interaction.edit_original_response(content=str(error),view=with_server_settings(None,interaction))
    except discord.HTTPException as error:
        if send_attempted and error.status in (400,403,404):service.community.matches.drop_pending_post(post.id)
        await interaction.edit_original_response(content='Delen niet bevestigd. Controleer deze chat voordat je opnieuw probeert.',view=None)
    except Exception as error:
        from .matches import MatchError
        message=str(error) if isinstance(error,(ValueError,MatchError)) else 'Delen niet bevestigd. Controleer deze chat voordat je opnieuw probeert.'
        await interaction.edit_original_response(content=message[:1900],view=None)


async def command(interaction,service,kind,first=0,last=0):
    from .identity import remember_interaction
    remember_interaction(service.hub,interaction)
    try:user=service.discord_user(interaction.user.id)
    except ValueError:
        view=discord.ui.View();view.add_item(discord.ui.Button(label='Profiel koppelen',url=settings.padelbot_public_url+'/#dashboard'))
        await interaction.response.send_message('Koppel je profiel om je eigen KUL-account te gebruiken.',view=with_server_settings(view,interaction),ephemeral=True);return
    if kind=='rules':
        view=discord.ui.View();view.add_item(discord.ui.Button(label='Autobooking instellen',url=settings.padelbot_public_url+'/#rules'))
        await interaction.response.send_message('Beheer je vaste momenten op je dashboard.',view=with_server_settings(view,interaction),ephemeral=True);return
    await interaction.response.defer(thinking=True,ephemeral=True)
    try:
        if kind=='slots':
            base = today()
            start, end = base + timedelta(days=first), base + timedelta(days=last)
            overview = await load_availability(service, user, start, end)
            view = (DaySlotsView(service, user, interaction.user.id, overview, start)
                    if first == last else SlotsView(service, user, interaction.user.id, overview))
            await interaction.edit_original_response(content=None, embed=view.embed(), view=with_server_settings(view,interaction))
            return
        else:
            result=await job(service,user,'bookings',{})
            content='\n'.join(f"{label(b)} · "+('Bevestigd' if b['confirmed'] else 'Betaling controleren') for b in result['bookings']) or 'Geen komende boekingen.'
            view=BookingsView(service,user,interaction.user.id,result['bookings'])
        await interaction.edit_original_response(content=content[:1950],view=with_server_settings(view,interaction))
    except Exception as error:await interaction.edit_original_response(content=str(error)[:1900],view=with_server_settings(None,interaction))
