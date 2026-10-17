"""Deliver private hosted-checkout links; no reservation or payment is retried."""
import asyncio,json,time,math
from datetime import datetime
from zoneinfo import ZoneInfo
import discord
from loguru import logger


async def deliver_once(bot,service):
    with service.store.conn() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("INSERT INTO platform_runtime VALUES('discord_heartbeat',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(time.time()),))
        row=c.execute("SELECT * FROM discord_deliveries WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
        if not row:return
        row=dict(row)
        c.execute("UPDATE discord_deliveries SET status='sending' WHERE id=? AND status='queued'",(row['id'],))
    try:
        user=service.store.user(row['user_id'])
        if not user or user['suspended'] or service.discord_id(user['id'])!=row['discord_id']:
            raise ValueError('Profiel is niet beschikbaar.')
        payload=json.loads(service.cipher().decrypt(row['payload'].encode()))
        if payload['user_id']!=row['user_id'] or payload['discord_id']!=row['discord_id']:
            raise ValueError('De ontvanger kon niet worden bevestigd.')
        kind=payload['kind'];view=None
        if kind=='notice':
            content='Je hebt geboekt.' if payload.get('confirmed') is True else 'Boeking niet bevestigd. Controleer je boekingen bij KUL voordat je opnieuw probeert.'
        else:
            target=datetime.fromisoformat(payload['target'].replace('Z','+00:00')).astimezone(ZoneInfo('Europe/Brussels'))
            content=f"Padel · {target:%d/%m om %H:%M}"
            if payload.get('amount') is not None:content+=f" · €{payload['amount']}"
            content+='\n'
            if kind=='payment':
                with service.store.conn() as c:
                    checkout=c.execute('SELECT status,created FROM private_payments WHERE user_id=? AND sale_id=?',
                        (row['user_id'],payload['sale_id'])).fetchone()
                if (not checkout or checkout['status']!='pending' or checkout['created']<time.time()-3600
                        or target.timestamp()<=time.time()):
                    with service.store.conn() as c:c.execute("UPDATE discord_deliveries SET status='cancelled',payload='',finished=? WHERE id=?",(time.time(),row['id']))
                    return
                from .payment import build_payment_url
                url=build_payment_url({'userInitiatedPaymentResult':{'bSuccess':True,'hostedPaymentPageUrl':payload['url']}})
                if not url:raise ValueError('Betaallink ontbreekt.')
                view=discord.ui.View();view.add_item(discord.ui.Button(label='Betalen',url=url))
                content+='Betaal om je boeking te bevestigen.'
            elif kind=='prepare':content+='Boeking wordt voorbereid...'
            elif kind=='booked':content='Je hebt geboekt.\n'+content.rstrip()
            elif kind=='reminder':
                # Never send an old queued reminder after a booking was cancelled,
                # moved, or started while the bot was offline.
                with service.store.conn() as c:
                    current=c.execute('SELECT confirmed,verified FROM booking_notifications WHERE user_id=? AND booking_id=? AND start_ts=?',
                        (row['user_id'],payload['booking_id'],target.timestamp())).fetchone()
                now=time.time()
                if not current or not current['confirmed'] or current['verified']<now-300 or not 0<target.timestamp()-now<=1800:
                    with service.store.conn() as c:c.execute("UPDATE discord_deliveries SET status='cancelled',payload='',finished=? WHERE id=?",(now,row['id']))
                    return
                minutes=math.ceil((target.timestamp()-now)/60)
                content=f"Padel begint over {minutes} {'minuut' if minutes==1 else 'minuten'}.\n"+content.rstrip()
            else:raise ValueError('Onbekend bericht.')
        if payload.get('message_id'):
            channel=bot.get_channel(payload['channel_id']) or await bot.fetch_channel(payload['channel_id'])
            sent=await channel.get_partial_message(payload['message_id']).edit(content=content,view=view,allowed_mentions=discord.AllowedMentions.none())
        else:
            recipient=bot.get_user(row['discord_id']) or await bot.fetch_user(row['discord_id'])
            sent=await recipient.send(content,view=view,allowed_mentions=discord.AllowedMentions.none())
        with service.store.conn() as c:
            c.execute("UPDATE discord_deliveries SET status='sent',payload='',finished=?,channel_id=?,message_id=? WHERE id=?",
                (time.time(),getattr(getattr(sent,'channel',None),'id',None),getattr(sent,'id',None),row['id']))
    except Exception as error:
        # Do not expose a provider URL or token through logs/history.
        logger.warning('Private checkout delivery failed: {}',type(error).__name__)
        with service.store.conn() as c:
            c.execute("UPDATE discord_deliveries SET status='failed',error=?,finished=? WHERE id=?",
                ('Discord-bericht niet afgeleverd. Controleer je privéberichten en betaal via KUL.',time.time(),row['id']))


async def run_deliveries(bot,service):
    with service.store.conn() as c:
        c.execute("UPDATE discord_deliveries SET status='unknown',error=? WHERE status='sending'",
            ('Controleer je Discord-berichten en je betaling bij KUL.',))
    while not bot.is_closed():
        try:await deliver_once(bot,service)
        except Exception as error:logger.warning('Discord delivery worker deferred: {}',type(error).__name__)
        await asyncio.sleep(2)
