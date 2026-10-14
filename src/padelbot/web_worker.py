"""One isolated KUL operation. Only deliberately selected fields leave this process."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import signal
import sys
from zoneinfo import ZoneInfo

from .config import settings
from .store import TokenStore


def initialise_key():
    store = TokenStore(settings.padelbot_key_file,settings.padelbot_token_file)
    if not store.key_file.exists():
        store.init_key()
    return store


def payment_summary(me: dict) -> dict:
    from .payment import has_direct_debit_mandate
    return {"balance":me.get("balance") or 0,
            "direct_debit":has_direct_debit_mandate(me) and int(me.get("directDebitSpec") or 0)>0,
            "manage_url":"https://usc.kuleuven.cloud/members/profile/myshopping#storedpaymethodcard"}


def execute(kind: str, payload: dict) -> dict:
    from .client import BackboneClient
    from .booking import find_slots_window, find_slots, book
    from .booking_manager import list_my_bookings
    from .checkout import slot_price
    from .rules_store import RulesStore
    initialise_key()
    if kind == "login":
        from .android import AndroidApprover
        from .auth import login_via_android
        if not settings.padelbot_android_profile_file.exists():
            raise ValueError("Koppel eerst je eigen virtuele authenticator via Instellingen.")
        tokens = login_via_android(AndroidApprover())
        initialise_key().save(tokens)
    with BackboneClient() as client:
        me = client.me()
        if kind in {"account","login"}:
            return {"connected":True,"name":str(me.get("firstName") or me.get("name") or "KUL-account"),
                    "payment":payment_summary(me)}
        if kind == "slots":
            start,end = int(payload["start"]),int(payload["end"])
            if not 0<=start<=end<=30:
                raise ValueError("Kies een dag of periode tussen 0 en 30.")
            today = datetime.now(ZoneInfo("Europe/Brussels"))
            slots = find_slots_window(client,start_date=today+timedelta(days=start),days=end-start+1)
            return {"slots":[{"product":s.bookable_product_id,"court":s.bookable_linked_product_id,
                              "start":s.start_iso,"end":s.end_iso,"price":s.price} for s in slots if s.start_dt>today]}
        if kind == "bookings":
            return {"bookings":[{"id":b.id,"sale_id":b.sale_id,"description":b.description,
                                 "start":b.start_iso,"end":b.end_iso,"paid":b.paid_for}
                                for b in list_my_bookings(client,member_id=int(me["id"]))]}
        if kind not in {"book","rule","quote"}:
            raise ValueError("Onbekende taak.")
        prefs = json.loads(os.environ.get("PADELWEB_PREFERENCES","{}"))
        if kind!="quote" and not prefs.get("automatic_payment"):
            raise ValueError("Schakel automatisch betalen in bij Instellingen voordat je boekt.")
        target = datetime.fromisoformat(payload["target"])
        if target.tzinfo is None:
            raise ValueError("Ongeldig tijdstip.")
        slots = find_slots(client,target_date=target)
        rules = RulesStore(settings.padelbot_rules_file)
        rule = None
        if kind == "rule":
            from .scheduler import find_rule_slot
            rule = rules.get_rule(int(payload["rule_id"]))
            if rule is None or not rule.enabled or rules.has_booking_for_day(rule.id,target.date()):
                return {"skipped":True}
            slot = find_rule_slot(slots,rule,target.date())
        else:
            slot = next((s for s in slots if s.start_dt==target and s.bookable_linked_product_id==int(payload["court"])),None)
        if slot is None:
            raise ValueError("Dit tijdstip is niet meer vrij. Vernieuw de beschikbaarheid.")
        amount = slot_price(client,slot,me)
        if kind=="quote":
            return {"amount":str(amount),"court":slot.bookable_linked_product_id,"target":slot.start_iso,
                    "payment":payment_summary(me)}
        maximum = Decimal(str(prefs.get("max_amount",0)))
        if amount>maximum or maximum<=0:
            raise ValueError("De prijs ligt boven jouw ingestelde maximum. Er is niets geboekt.")
        # Only KUL balance and an already-authorised mandate can be unattended.
        # This worker never loads a legacy encrypted card or accepts browser card data.
        summary = payment_summary(me)
        if Decimal(str(summary["balance"]))<amount and not summary["direct_debit"]:
            raise ValueError("Vul eerst je KUL-saldo aan of koppel een domiciliëring in KU Leuven Sport.")
        if kind == "book" and amount != Decimal(str(payload["amount"])):
            raise ValueError("De prijs is gewijzigd. Vernieuw het overzicht en bevestig opnieuw.")
        result = book(client,slot,member_id=int(me["id"]),me=me,expected_amount=amount,label=rule.notes if rule else "Sigmadvisor")
        rules.record(rule_id=rule.id if rule else None,success=result.paid,sale_id=result.sale_id,
            booking_id=result.booking_id,target_slot_iso=slot.start_dt.astimezone(ZoneInfo("Europe/Brussels")).isoformat(),
            error=None if result.paid else "Betaling nog niet bevestigd; controleer KU Leuven Sport.")
        return {"sale_id":result.sale_id,"booking_id":result.booking_id,"paid":result.paid,
                "amount":str(amount),"payment_url":result.payment_url,
                "message":"Geboekt en betaald." if result.paid else "Reservatie aangemaakt. Controleer de betaling in KU Leuven Sport."}


def main():
    # SIGTERM unwinds finally blocks, including emulator cleanup.
    signal.signal(signal.SIGTERM,lambda *_:sys.exit(1))
    message = json.loads(sys.stdin.buffer.read())
    try:
        result = execute(message["kind"],message["payload"])
    except (ValueError,) as error:
        result = {"error":str(error)}
    except BaseException:
        # Provider exceptions may contain account identifiers, challenge links or cookies.
        result = {"error":"KUL kon deze taak niet afronden. Controleer je koppeling, saldo en boekingen voor je opnieuw probeert."}
    Path(os.environ["PADELWEB_RESULT"]).write_text(json.dumps(result),encoding="utf-8")


if __name__ == "__main__":
    main()
