"""Thin Discord views over the same tenant queue and booking code as the website."""
import asyncio,time
from datetime import datetime
from zoneinfo import ZoneInfo
import discord

from .config import settings


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
            self.user=self.service.discord_user(interaction.user.id)
        except ValueError as error:
            await interaction.response.send_message(str(error),ephemeral=True);return False
        return True
    def button(self,text,callback,**kwargs):
        b=discord.ui.Button(label=text,**kwargs);b.callback=callback;self.add_item(b)


class SlotsView(ProfileView):
    def __init__(self,service,user,discord_id,slots,*,page=0):
        super().__init__(service,user,discord_id);self.slots=slots;self.page=page
        chunk=slots[page*25:(page+1)*25]
        if chunk:
            select=discord.ui.Select(placeholder='Kies je moment',options=[discord.SelectOption(label=label(s)[:100],value=str(i+page*25)) for i,s in enumerate(chunk)])
            select.callback=self.choose;self.add_item(select);self.select=select
        if page:self.button('Vorige',self.previous,row=1)
        if (page+1)*25<len(slots):self.button('Volgende',self.next,row=1)
        self.add_item(discord.ui.Button(label='Dashboard',url=settings.padelbot_public_url))
    async def previous(self,i):await i.response.edit_message(view=SlotsView(self.service,self.user,self.discord_id,self.slots,page=self.page-1))
    async def next(self,i):await i.response.edit_message(view=SlotsView(self.service,self.user,self.discord_id,self.slots,page=self.page+1))
    async def choose(self,interaction):
        self.busy=True;await interaction.response.defer()
        slot=self.slots[int(self.select.values[0])]
        try:
            quote=await job(self.service,self.user,'quote',{'target':slot['start'],'product':slot['product'],'court':slot['court'],'amount':1})
            view=ConfirmView(self.service,self.user,self.discord_id,slot,quote)
            mode=self.service.store.preferences(self.user['id']).get('payment_mode','kul')
            await interaction.edit_original_response(content=f"**{label(slot)} · €{quote['amount']}**\n"+('Betaallink hier, op je dashboard en in Discord. Voldoende KUL-saldo gebruiken we eerst.' if mode=='discord' else 'Betalen via je KUL-kaart. Voldoende KUL-saldo gebruiken we eerst.'),view=view)
        except Exception as error:
            await interaction.edit_original_response(content=str(error),view=None)


class ConfirmView(ProfileView):
    def __init__(self,service,user,discord_id,slot,quote):
        super().__init__(service,user,discord_id,timeout=120);self.slot=slot;self.quote=quote;self.expires=time.monotonic()+120
        self.button('Bevestigen · €'+str(quote['amount']),self.confirm,style=discord.ButtonStyle.success)
        self.button('Sluiten',self.close)
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
        await interaction.edit_original_response(content=content,view=view)


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
        await interaction.response.edit_message(view=BookingsView(self.service,self.user,self.discord_id,self.bookings,page=self.page-1))
    async def next(self,interaction):
        await interaction.response.edit_message(view=BookingsView(self.service,self.user,self.discord_id,self.bookings,page=self.page+1))
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
    await interaction.response.defer(thinking=False)
    post=None;send_attempted=False
    try:
        await job(service,current,'bookings',{})
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
        await interaction.response.send_message('Koppel je profiel om je eigen KUL-account te gebruiken.',view=view,ephemeral=True);return
    if kind=='rules':
        view=discord.ui.View();view.add_item(discord.ui.Button(label='Autobooking instellen',url=settings.padelbot_public_url+'/#rules'))
        await interaction.response.send_message('Beheer je vaste momenten op je dashboard.',view=view,ephemeral=True);return
    await interaction.response.defer(thinking=True,ephemeral=True)
    try:
        if kind=='slots':
            result=await job(service,user,'slots',{'start':first,'end':last})
            view=SlotsView(service,user,interaction.user.id,result['slots'])
            content='Kies een vrij moment.' if result['slots'] else 'Geen vrije momenten.'
        else:
            result=await job(service,user,'bookings',{})
            content='\n'.join(f"{label(b)} · "+('Bevestigd' if b['confirmed'] else 'Betaling controleren') for b in result['bookings']) or 'Geen komende boekingen.'
            view=BookingsView(service,user,interaction.user.id,result['bookings'])
        await interaction.edit_original_response(content=content[:1950],view=view)
    except Exception as error:await interaction.edit_original_response(content=str(error)[:1900],view=None)
