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
        try:self.user=self.service.discord_user(interaction.user.id)
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
            await interaction.edit_original_response(content=f"**{label(slot)} · €{quote['amount']}**\n"+('Je krijgt de betaallink privé via Discord.' if mode=='discord' else 'Betalen via je gekoppelde KUL-betaalmethode.'),view=view)
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
        except Exception as error:content=str(error)
        await interaction.edit_original_response(content=content,view=None)


async def command(interaction,service,kind,first=0,last=0):
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
            view=discord.ui.View();view.add_item(discord.ui.Button(label='Boekingen beheren',url=settings.padelbot_public_url+'/#bookings'))
        await interaction.edit_original_response(content=content[:1950],view=view)
    except Exception as error:await interaction.edit_original_response(content=str(error)[:1900],view=None)
