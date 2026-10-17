"""Bounded shared booking checks for checkout confirmation and reminders.

Only profiles with a pending checkout or a known imminent reservation are read.
The normal tenant worker owns every KUL request and starts Android only if needed.
"""
import asyncio
import time

from loguru import logger


def refresh_due_bookings(service,*,now=None):
    now=time.time() if now is None else now
    queued=[]
    for user_id in service.users_needing_booking_refresh(now=now):
        user=service.store.user(user_id)
        if not user or user['suspended']:continue
        with service.store.conn() as c:
            # An unlinked profile needs no Discord reminder checks. Its own
            # pending website checkout still needs payment confirmation.
            if not service.discord_id(user_id) and not c.execute(
                "SELECT 1 FROM private_payments WHERE user_id=? AND status='pending' AND created>? LIMIT 1",
                (user_id,now-3600)).fetchone():continue
            if c.execute("SELECT 1 FROM jobs WHERE user_id=? AND status IN ('queued','running') LIMIT 1",(user_id,)).fetchone():
                continue
            previous=c.execute("SELECT status,created FROM jobs WHERE user_id=? AND kind='bookings' ORDER BY created DESC LIMIT 1",(user_id,)).fetchone()
            # Authentication failures must not repeatedly start an idle emulator.
            cooldown=900 if previous and previous['status'] in {'failed','unknown'} else 120
            if previous and previous['created']>now-cooldown:continue
            c.execute('BEGIN IMMEDIATE')
            key='discord_bookings_refresh:'+user_id
            leased=c.execute('SELECT value FROM platform_runtime WHERE key=?',(key,)).fetchone()
            if leased and float(leased[0])>now-120:continue
            c.execute('INSERT INTO platform_runtime VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(now)))
        try:
            queued.append(service.store.enqueue(user_id,'bookings',{},dedup=f'discord-verify:{user_id}:{int(now)//120}'))
        except ValueError:
            # Foreground work takes priority. The next bounded tick can try again.
            continue
    return queued


async def run_booking_lifecycle(bot,service):
    while not bot.is_closed():
        try:
            await asyncio.to_thread(refresh_due_bookings,service)
            await asyncio.to_thread(service.queue_due_reminders)
        except Exception as error:
            logger.warning('Shared booking reminders deferred: {}',type(error).__name__)
        await asyncio.sleep(20)
