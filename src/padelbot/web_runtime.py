"""A bounded queue, tenant-specific subprocesses, and one shared Android worker."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from zoneinfo import ZoneInfo

from .web_store import PlatformStore, READ_JOB_KINDS
from .web_community import CommunityBookings


def tenant_root(store: PlatformStore, user: dict) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", user["id"]):
        raise ValueError("Invalid profile identifier")
    root = store.root / "profiles" / user["id"]
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def tenant_env(store: PlatformStore, user: dict) -> dict[str, str]:
    # The legacy bot has module-level settings. Each tenant runs in a new process;
    # no settings object, cookie jar or card store is switched in a shared process.
    from .config import Settings
    base = Settings()
    root = tenant_root(store, user)
    if user["owner"]:
        config = base.model_dump()
    else:
        config = Settings(_env_file=None).model_dump()
        # Explicitly replace all identity/material values even if the service environment has defaults.
        config.update({"padelbot_key_file": root/"master.key", "padelbot_token_file": root/"tokens.enc",
                       "padelbot_storage_file": root/"session.json", "padelbot_card_file": root/"unused-card.enc",
                       "padelbot_rules_file": root/"rules.sqlite3", "padel_member_id": 0,
                       "padelbot_android_profile_file": root/"android-profile.json",
                       "padelbot_android_pin_file": root/"pin.enc", "padelbot_android_failure_file": root/"failure.json",
                       "padelbot_android_emulator_home": root/"emulators",
                       "padelbot_android_emulator_name": "Padel_"+user["id"],
                       "padelbot_android_device_id": "", "padelbot_android_autologin": (root/"android-profile.json").exists()})
    config.update({"discord_bot_token": "", "discord_webhook_url": "", "discord_owner_id": 0,
                   "discord_guild_id": 0, "kul_card_holder": "", "kul_card_number": "",
                   "kul_card_exp_month": 0, "kul_card_exp_year": 0, "kul_card_cvv": "",
                   # The web app never reads legacy stored card/CVC data.
                   "padelbot_card_file": root/"unused-card.enc",
                   "padelbot_android_adb": base.padelbot_android_adb,
                   "padelbot_android_serial": "emulator-5554", "padelbot_android_emulator_autostart": True,
                   "padelbot_android_emulator_memory_mb": 2560, "padelbot_android_emulator_cores": 1,
                   "padelbot_auth_remote_command": base.padelbot_auth_remote_command,
                   "padelbot_emulator_lock_file": base.padelbot_emulator_lock_file.resolve()})
    env = {k:v for k,v in os.environ.items() if k in {"PATH","HOME","SYSTEMROOT","WINDIR","TEMP","TMP","LD_LIBRARY_PATH","ANDROID_USER_HOME","ANDROID_EMULATOR_FEATURES","PLAYWRIGHT_BROWSERS_PATH"}}
    env["PADELBOT_ENV_FILE"] = ""
    for key, value in config.items():
        if value is not None:
            # Identity files stay anchored to the service directory even if a
            # child starts from another checkout or working directory.
            if isinstance(value,Path):value=value.resolve()
            env[key.upper()] = str(value) if not isinstance(value,bool) else str(value).lower()
    env["PADELWEB_PROFILE_ROOT"] = str(root)
    env["PADELWEB_PREFERENCES"] = json.dumps(store.preferences(user["id"]))
    env['PADELWEB_DB']=str(store.path)
    env['PADELWEB_USER_ID']=user['id']
    env['PADELWEB_HUB_DB']=str(base.padelbot_rules_file.resolve())
    env['PADELWEB_TELEMETRY_DB']=str((store.root/'telemetry.sqlite3').resolve())
    return env


def rules_for(store: PlatformStore, user: dict):
    from .rules_store import RulesStore
    env = tenant_env(store,user)
    return RulesStore(Path(env["PADELBOT_RULES_FILE"]))


def rule_window(rule,now:datetime):
    """The API accepts starts up to exactly 168 hours ahead, including across DST."""
    tz=ZoneInfo('Europe/Brussels')
    local=now.astimezone(tz)
    day=local.date()+timedelta(days=(rule.day_of_week-local.weekday())%7)
    first=datetime.strptime(rule.time_local,'%H:%M').time()
    last=datetime.strptime(rule.end_time_local or rule.time_local,'%H:%M').time()
    end=datetime.combine(day,last,tz)
    if end<=local:
        day+=timedelta(days=7)
        end=datetime.combine(day,last,tz)
    target=datetime.combine(day,first,tz)
    opens=target.astimezone(timezone.utc)-timedelta(hours=168)
    return target,end,opens


class JobRunner:
    def __init__(self, store: PlatformStore):
        self.store = store
        self.process = None
        self.running = {}
        self.community = CommunityBookings(store)
        from .drafts import DraftStore
        self.drafts=DraftStore(self.community)

    def recover(self):
        # Only read-only work can be replayed after a process/service restart.
        with self.store.conn() as c:
            running=[dict(row) for row in c.execute("SELECT * FROM jobs WHERE status='running'")]
        for row in running:
            if row['kind'] in READ_JOB_KINDS:
                with self.store.conn() as c:
                    c.execute("UPDATE jobs SET status='queued',result=NULL,finished=NULL WHERE id=?",(row['id'],))
            else:
                self.finish(row['id'],'unknown',{'error':'De server werd herstart. Controleer je boekingen voor je opnieuw probeert.'})
        with self.store.conn() as c:
            interrupted=[row['id'] for row in c.execute("SELECT id FROM jobs WHERE status='waiting_auth' AND auth_job_id=id AND auth_started IS NOT NULL")]
        for job_id in interrupted:
            # The challenge/PIN may have reached KUL before the process died.
            # Never automatically repeat a native-auth phase after interruption.
            self.finish(job_id,'unknown',{'error':'De KUL-aanmelding werd onderbroken. Controleer je koppeling voor je opnieuw probeert.'})
        self.drafts.recover()

    async def run(self):
        self.recover()
        try:
            while True:
                await self.tick()
                await asyncio.sleep(.25)
        finally:
            tasks=[entry['task'] for entry in self.running.values()]
            for task in tasks:task.cancel()
            if tasks:await asyncio.gather(*tasks,return_exceptions=True)
            self.running.clear()

    def _job_user(self,row):
        user=self.store.user(row['user_id'])
        if not user or (user['suspended'] and row['kind']!='draft_release'):
            self.finish(row['id'],'cancelled',{'error':'Profiel is gepauzeerd.'})
            return None
        if row['kind']=='cancel_booking':
            from .platform_service import PlatformService
            if PlatformService(self.store,self.community).access_status(user['id'])['suspended']:
                self.finish(row['id'],'cancelled',{'error':'Profiel is gepauzeerd.','safe_to_retry':True})
                return None
        if row['kind'] in {'book','rule','community_join','draft_refresh'}:
            try:self.store.assert_writable(user['id'])
            except ValueError as error:
                self.finish(row['id'],'cancelled',{'error':str(error)})
                return None
        if row['kind']=='login' and json.loads(row['payload']).get('preflight') is True:
            from .platform_service import PlatformService
            if not PlatformService(self.store,self.community).can_autobook(user['id']):
                self.finish(row['id'],'cancelled',{'error':'Autobooking staat niet aan met een gekoppelde kaart.'})
                return None
        source=user
        if row['kind']=='community_join':
            try:
                match=self.community.host(json.loads(row['payload'])['match_id'])
                source=self.store.user(match['user_id'])
                if not source or source['suspended']:raise ValueError('Host unavailable')
            except (ValueError,KeyError):
                self.finish(row['id'],'failed',{'error':'Deze online boeking is niet meer beschikbaar.'})
                return None
        return user,source

    def _resolve_waiters(self):
        with self.store.conn() as c:
            rows=[dict(row) for row in c.execute("""SELECT j.*,a.status AS auth_status,a.user_id AS auth_user,
                u.suspended AS user_suspended,u.id AS existing_user FROM jobs j
                LEFT JOIN jobs a ON a.id=j.auth_job_id LEFT JOIN users u ON u.id=j.user_id
                WHERE j.status='waiting_auth' AND
                    (j.auth_job_id IS NULL OR j.auth_job_id!=j.id OR u.suspended=1 OR u.id IS NULL)""")]
        for row in rows:
            if not row['existing_user'] or row['user_suspended']:
                self.finish(row['id'],'cancelled',{'error':'Profiel is gepauzeerd.'})
                continue
            if row['auth_user']==row['user_id'] and row['auth_status']=='done':
                with self.store.conn() as c:
                    c.execute("UPDATE jobs SET status='queued',auth_job_id=NULL,result=NULL WHERE id=? AND status='waiting_auth'",(row['id'],))
            elif row['auth_user']!=row['user_id'] or row['auth_status'] not in {'queued','running'}:
                self.finish(row['id'],'failed',{'error':'KUL kon niet opnieuw aanmelden. Controleer je KUL-koppeling.'})

    async def tick(self):
        for job_id,entry in list(self.running.items()):
            if entry['task'].done():
                outcome=await asyncio.gather(entry['task'],return_exceptions=True)
                if isinstance(outcome[0],BaseException):
                    self.finish(job_id,'unknown',{'error':'Taak onderbroken. Controleer de boekingsstatus voor je opnieuw probeert.'})
                self.running.pop(job_id,None)
        self._resolve_waiters()
        read_count=sum(entry['lane']=='read' for entry in self.running.values())
        serial_count=sum(entry['lane']=='serial' for entry in self.running.values())
        if read_count>=2 and serial_count>=1:return
        busy=set().union(*(entry['keys'] for entry in self.running.values())) if self.running else set()
        with self.store.conn() as c:
            rows=[dict(row) for row in c.execute("SELECT * FROM jobs WHERE status IN ('queued','waiting_auth') ORDER BY created,id")]
        blocked={}
        for row in rows:
            if read_count>=2 and serial_count>=1:break
            if row['id'] in self.running:continue
            keys={row['user_id']}
            if row['kind']=='community_join':
                try:keys.add(self.community.host(json.loads(row['payload'])['match_id'])['user_id'])
                except (ValueError,KeyError):
                    self.finish(row['id'],'failed',{'error':'Deze online boeking is niet meer beschikbaar.'})
                    continue
            auth_stage=row['status']=='waiting_auth' and row['auth_job_id']==row['id']
            # The awaited login may pass followers queued behind its parked
            # predecessor; otherwise a full per-profile queue would deadlock.
            dependency_bypass=all(not blocked.get(key) or (blocked[key][0]['status']=='waiting_auth' and blocked[key][0]['auth_job_id']==row['id']) for key in keys)
            earlier=any(blocked.get(key) for key in keys)
            lane='read' if row['kind'] in READ_JOB_KINDS and not auth_stage else 'serial'
            waiting_external=row['status']=='waiting_auth' and not auth_stage
            capacity=(read_count<2 if lane=='read' else serial_count<1)
            if waiting_external or busy.intersection(keys) or (earlier and not dependency_bypass) or not capacity:
                for key in keys:blocked.setdefault(key,[]).append(row)
                continue
            users=self._job_user(row)
            if not users:continue
            user,source=users
            if auth_stage:
                with self.store.conn() as c:
                    changed=c.execute("UPDATE jobs SET auth_started=? WHERE id=? AND status='waiting_auth' AND auth_job_id=id AND auth_started IS NULL",(time.time(),row['id'])).rowcount
                if not changed:
                    self.finish(row['id'],'unknown',{'error':'De KUL-aanmelding werd onderbroken. Controleer je koppeling voor je opnieuw probeert.'})
                    continue
            else:
                with self.store.conn() as c:
                    changed=c.execute("UPDATE jobs SET status='running',started=COALESCE(started,?) WHERE id=? AND status='queued'",(time.time(),row['id'])).rowcount
                if not changed:continue
            self.running[row['id']]={'task':asyncio.create_task(self._execute_job(row,user,source,auth_stage=auth_stage)), 'keys':keys,'lane':lane}
            busy.update(keys)
            if lane=='read':read_count+=1
            else:serial_count+=1

    def _park_read(self,row):
        if row['kind'] not in READ_JOB_KINDS or row['reauth_attempts']>=1:
            self.finish(row['id'],'failed',{'error':'KUL vraagt opnieuw aanmelden. Controleer je KUL-koppeling.'})
            return
        with self.store.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            login=c.execute("SELECT id FROM jobs WHERE user_id=? AND kind='login' AND status IN ('queued','running') ORDER BY created LIMIT 1",(row['user_id'],)).fetchone()
            dependency=login['id'] if login else row['id']
            c.execute("UPDATE jobs SET status='waiting_auth',auth_job_id=?,reauth_attempts=reauth_attempts+1,result=NULL WHERE id=? AND status='running'",(dependency,row['id']))

    async def _stop_process(self,process):
        if process.returncode is not None:return
        process.terminate()
        try:await asyncio.wait_for(process.wait(),timeout=25)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    async def _execute_job(self,row,user,source_user,*,auth_stage=False):
        output=tenant_root(self.store,user)/('job-'+row['id']+'.json')
        output.unlink(missing_ok=True)
        env=tenant_env(self.store,source_user)
        env['PADELWEB_HUB_DB']=str(self.community.matches.path.resolve())
        env['PADELWEB_RESULT']=str(output)
        env['PADELWEB_JOB_ID']=row['id']
        kind='login' if auth_stage else row['kind']
        env['PADELWEB_DEFER_AUTH']='true' if kind in READ_JOB_KINDS else 'false'
        preflight=kind=='login' and json.loads(row['payload']).get('preflight') is True
        env['PADELWEB_AUTH_CONTEXT']='renewal' if auth_stage or preflight else 'login'
        process=None
        try:
            process=await asyncio.create_subprocess_exec(sys.executable,'-m','padelbot.web_worker',env=env,
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
            message=json.dumps({'kind':kind,'payload':{} if auth_stage else json.loads(row['payload'])}).encode()
            await asyncio.wait_for(process.communicate(message),timeout=720)
            if not output.exists():
                self.finish(row['id'],'unknown',{'error':'Taak onderbroken. Controleer de boekingsstatus voor je opnieuw probeert.'})
                return
            result=json.loads(output.read_text(encoding='utf-8'))
            if result.get('reauth_required') is True:
                self._park_read(row)
            elif auth_stage and 'error' not in result:
                if result.get('payment'):
                    from .platform_service import PlatformService
                    PlatformService(self.store,self.community).remember_payment(row['user_id'],result['payment'],checked=result.get('payment_checked_at') or row['started'] or row['created'])
                with self.store.conn() as c:
                    c.execute("UPDATE jobs SET status='queued',auth_job_id=NULL,result=NULL WHERE id=? AND status='waiting_auth'",(row['id'],))
            else:self.finish(row['id'],'unknown' if result.get('cancellation_unknown') is True else 'failed' if 'error' in result else 'done',result)
        except asyncio.TimeoutError:
            if process:await self._stop_process(process)
            self.finish(row['id'],'unknown',{'error':'Taak duurde te lang. Controleer de boekingsstatus voor je opnieuw probeert.'})
        except asyncio.CancelledError:
            if process:await self._stop_process(process)
            raise
        except Exception:
            self.finish(row['id'],'failed',{'error':'Taak kon niet worden uitgevoerd. Neem contact op met de beheerder.'})
        finally:output.unlink(missing_ok=True)

    def finish(self, job_id, status, result):
        with self.store.conn() as c:
            job=c.execute('SELECT user_id,kind,payload,created,started FROM jobs WHERE id=?',(job_id,)).fetchone()
        if job and job['kind']=='bookings' and status=='done' and isinstance(result.get('bookings'),list):
            from .platform_service import PlatformService
            service=PlatformService(self.store,self.community)
            observed=result.get('bookings_checked_at') or job['started'] or job['created']
            result['bookings']=service.filter_stale_bookings(job['user_id'],result['bookings'],observed=observed)
        if job and status in {'failed','unknown','cancelled'}:
            payload=json.loads(job['payload'])
            if job['kind']=='draft_refresh':
                self.drafts.failed(payload['draft_id'],'KUL-controle onderbroken. Controleer je draft en winkelwagen.')
            elif job['kind']=='book' and payload.get('draft_id'):
                self.drafts.uncertain_conversion(payload['draft_id'])
        if job and job['kind']=='cancel_booking' and status=='done' and result.get('cancelled') is True:
            from .platform_service import PlatformService
            from loguru import logger
            service=PlatformService(self.store,self.community)
            if result.get('payment'):
                try:service.remember_payment(job['user_id'],result['payment'],checked=result.get('payment_checked_at') or job['started'] or job['created'])
                except Exception as error:
                    result['refresh_required']=True
                    logger.warning('Cancellation balance refresh deferred: {}',type(error).__name__)
            try:
                service.confirm_cancelled_booking(job['user_id'],json.loads(job['payload'])['booking_id'])
                if isinstance(result.get('bookings'),list):
                    observed=result.get('bookings_checked_at') or job['started'] or job['created']
                    result['bookings']=service.filter_stale_bookings(job['user_id'],result['bookings'],observed=observed)
                    self.community.sync(job['user_id'],result['bookings'],device_verified=result.get('device_verified') is True)
                    service.sync_verified_bookings(job['user_id'],result['bookings'],device_verified=result.get('device_verified') is True)
                    service.queue_due_reminders()
            except Exception as error:
                # KUL has already confirmed the write. Never expose a retry just
                # because a local notification or roster update was interrupted.
                result['refresh_required']=True
                logger.warning('Cancellation synchronization deferred: {}',type(error).__name__)
        if job and job['kind']=='bookings' and status=='done' and isinstance(result.get('bookings'),list):
            self.community.sync(job['user_id'],result.get('bookings',[]),device_verified=result.get('device_verified') is True)
        if job and status=='done' and job['kind'] in {'book','rule','bookings'}:
            from .platform_service import PlatformService
            from loguru import logger
            try:
                service=PlatformService(self.store,self.community)
                if job['kind']=='bookings' and isinstance(result.get('bookings'),list):
                    service.sync_verified_bookings(job['user_id'],result.get('bookings',[]),device_verified=result.get('device_verified') is True)
                    service.queue_due_reminders()
                elif result.get('paid') is True and result.get('booking_id'):
                    service.queue_confirmed_booking(job['user_id'],result['booking_id'],result.get('target') or json.loads(job['payload'])['target'],result.get('amount'))
            except Exception as error:
                # A notification failure must never turn a confirmed paid write
                # into a failed job that invites another booking attempt.
                logger.warning('Booking notification deferred: {}',type(error).__name__)
        if job and job['kind'] in {'login','account','quote'} and status=='done' and result.get('payment'):
            from .platform_service import PlatformService
            PlatformService(self.store,self.community).remember_payment(job['user_id'],result['payment'],checked=result.get('payment_checked_at') or job['started'] or job['created'])
        with self.store.conn() as c:
            c.execute("UPDATE jobs SET status=?,result=?,finished=? WHERE id=?",(status,json.dumps(result),time.time(),job_id))
            if status=='done' and result.get('waiting') is True:
                # The worker stopped before any reservation/payment write. Only this
                # read-only outcome may be checked again. Interrupted writes retain dedup.
                c.execute("UPDATE jobs SET dedup=NULL WHERE id=? AND kind='rule'",(job_id,))
            elif status=='failed' and result.get('safe_to_retry') is True:
                c.execute("UPDATE jobs SET dedup=NULL WHERE id=? AND kind IN ('book','rule','cancel_booking')",(job_id,))
            elif status=='cancelled' and result.get('safe_to_retry') is True:
                c.execute("UPDATE jobs SET dedup=NULL WHERE id=? AND kind='cancel_booking'",(job_id,))
        if job:
            from .telemetry import record_job
            record_job(self.store.root/'telemetry.sqlite3',user_id=job['user_id'],job_id=job_id,kind=job['kind'],
                created=job['created'],started=job['started'] or time.time(),finished=time.time(),status=status)

    async def scheduler(self):
        from .platform_service import PlatformService
        service=PlatformService(self.store,self.community)
        tz = ZoneInfo("Europe/Brussels")
        last_checked={}
        preflight_checked={}
        snapshot=[];next_refresh=0
        while True:
            now = datetime.now(tz)
            if time.monotonic()>=next_refresh:
                with self.store.conn() as c:
                    users=[dict(r) for r in c.execute("SELECT * FROM users WHERE suspended=0 AND restricted=0")]
                snapshot=[]
                for user in users:
                    prefs=self.store.preferences(user['id'])
                    if not service.can_autobook(user['id']) or float(prefs.get('max_amount',0))<=0:continue
                    rules=rules_for(self.store,user)
                    snapshot.append((user,rules,rules.list_rules(enabled_only=True)))
                next_refresh=time.monotonic()+15
            for user,rules,enabled_rules in snapshot:
                for rule in enabled_rules:
                    target,end,opens=rule_window(rule,now)
                    occurrence=f"{user['id']}:{rule.id}:{target.date()}"
                    preflight=f"auth-preflight:{user['id']}:{int(opens.timestamp())}"
                    if opens-timedelta(minutes=15)<=now.astimezone(timezone.utc)<opens and preflight not in preflight_checked:
                        try:
                            self.store.enqueue(user['id'],'login',{'preflight':True},dedup=preflight)
                            preflight_checked[preflight]=time.monotonic()
                        except ValueError:pass
                    if opens<=now.astimezone(timezone.utc)<end.astimezone(timezone.utc):
                        if time.monotonic()-last_checked.get(occurrence,-1000)<120 or rules.has_booking_for_day(rule.id,target.date()):
                            continue
                        try:
                            self.store.enqueue(user["id"],"rule",{"rule_id":rule.id,"target":target.isoformat()},dedup=occurrence)
                            last_checked[occurrence]=time.monotonic()
                        except ValueError:
                            pass
            for draft in self.drafts.due():
                try:
                    self.store.enqueue(draft['profile_id'],draft['kind'],{'draft_id':draft['id']},
                        dedup=f"{draft['kind']}:{draft['id']}:{int(time.time())//300}")
                except ValueError:
                    self.drafts.failed(draft['id'],'De wachtrij is vol. Controleer je draft.')
            if len(last_checked)>1000:
                cutoff=time.monotonic()-86400
                last_checked={key:stamp for key,stamp in last_checked.items() if stamp>cutoff}
            if len(preflight_checked)>1000:
                cutoff=time.monotonic()-86400
                preflight_checked={key:stamp for key,stamp in preflight_checked.items() if stamp>cutoff}
            await asyncio.sleep(1)
