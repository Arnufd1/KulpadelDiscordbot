"""A bounded queue, tenant-specific subprocesses, and one shared Android worker."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import sys
import time
from zoneinfo import ZoneInfo

from .web_store import PlatformStore


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
                   "padelbot_emulator_lock_file": base.padelbot_emulator_lock_file.resolve()})
    env = {k:v for k,v in os.environ.items() if k in {"PATH","HOME","SYSTEMROOT","WINDIR","TEMP","TMP","LD_LIBRARY_PATH","ANDROID_USER_HOME","ANDROID_EMULATOR_FEATURES","PLAYWRIGHT_BROWSERS_PATH"}}
    env["PADELBOT_ENV_FILE"] = ""
    for key, value in config.items():
        if value is not None:
            env[key.upper()] = str(value) if not isinstance(value,bool) else str(value).lower()
    env["PADELWEB_PROFILE_ROOT"] = str(root)
    env["PADELWEB_PREFERENCES"] = json.dumps(store.preferences(user["id"]))
    return env


def rules_for(store: PlatformStore, user: dict):
    from .rules_store import RulesStore
    env = tenant_env(store,user)
    return RulesStore(Path(env["PADELBOT_RULES_FILE"]))


class JobRunner:
    def __init__(self, store: PlatformStore):
        self.store = store
        self.process = None

    async def run(self):
        # Writes interrupted by restart are not replayed: their remote outcome may be unknown.
        with self.store.conn() as c:
            c.execute("UPDATE jobs SET status='unknown', result=?,finished=? WHERE status='running'",
                      (json.dumps({"error":"De server werd herstart. Controleer je boekingen vóór je opnieuw probeert."}),time.time()))
        while True:
            with self.store.conn() as c:
                row = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY CASE kind WHEN 'rule' THEN 0 ELSE 1 END,created LIMIT 1").fetchone()
            if not row:
                await asyncio.sleep(0.5)
                continue
            user = self.store.user(row["user_id"])
            if not user or user["suspended"]:
                self.finish(row["id"],"cancelled",{"error":"Profiel is gepauzeerd."})
                continue
            with self.store.conn() as c:
                c.execute("UPDATE jobs SET status='running' WHERE id=?",(row["id"],))
            output = tenant_root(self.store,user) / ("job-"+row["id"]+".json")
            env = tenant_env(self.store,user)
            env["PADELWEB_RESULT"] = str(output)
            try:
                self.process = await asyncio.create_subprocess_exec(sys.executable,"-m","padelbot.web_worker",
                    env=env,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
                message = json.dumps({"kind":row["kind"],"payload":json.loads(row["payload"])}).encode()
                await asyncio.wait_for(self.process.communicate(message),timeout=720)
                result = json.loads(output.read_text()) if output.exists() else {"error":"Taak onderbroken. Controleer de boekingsstatus voor je opnieuw probeert."}
                self.finish(row["id"],"failed" if "error" in result else "done",result)
            except asyncio.TimeoutError:
                self.process.terminate()
                await self.process.wait()
                self.finish(row["id"],"unknown",{"error":"Taak duurde te lang. Controleer de boekingsstatus voor je opnieuw probeert."})
            except asyncio.CancelledError:
                if self.process and self.process.returncode is None:
                    self.process.terminate()
                    await self.process.wait()
                raise
            except Exception:
                self.finish(row["id"],"failed",{"error":"Taak kon niet worden uitgevoerd. Neem contact op met de beheerder."})
            finally:
                output.unlink(missing_ok=True)
                self.process = None

    def finish(self, job_id, status, result):
        with self.store.conn() as c:
            c.execute("UPDATE jobs SET status=?,result=?,finished=? WHERE id=?",(status,json.dumps(result),time.time(),job_id))

    async def scheduler(self):
        from .scheduler import compute_next_fire
        tz = ZoneInfo("Europe/Brussels")
        while True:
            now = datetime.now(tz)
            with self.store.conn() as c:
                users = [dict(r) for r in c.execute("SELECT * FROM users WHERE owner=0 AND suspended=0")]
            for user in users:
                prefs = self.store.preferences(user["id"])
                if not prefs.get("automatic_payment") or float(prefs.get("max_amount",0))<=0:
                    continue
                for rule in rules_for(self.store,user).list_rules(enabled_only=True):
                    spec = compute_next_fire(rule,now=now-timedelta(minutes=5))
                    delay = (spec.fire_at-now).total_seconds()
                    if -300 <= delay <= 1:
                        try:
                            self.store.enqueue(user["id"],"rule",{"rule_id":rule.id,"target":spec.target_slot_local.isoformat()},
                                dedup=f"{user['id']}:{rule.id}:{spec.target_slot_local.date()}")
                        except ValueError:
                            pass
            await asyncio.sleep(1)
