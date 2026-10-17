"""One isolated KUL operation. Only deliberately selected fields leave this process."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
from zoneinfo import ZoneInfo

from .config import settings
from .store import TokenStore


_reservation_started=False
_prepared_delivery=None
_prepared_service=None


def current_preferences() -> dict:
    if os.environ.get('PADELWEB_DB') and os.environ.get('PADELWEB_USER_ID'):
        user=assert_writable_profile()
        return json.loads(user['preferences'])
    return json.loads(os.environ.get('PADELWEB_PREFERENCES','{}'))


def assert_writable_profile():
    """Re-read moderation at the reservation boundary, including queued jobs."""
    from .platform_service import PlatformService
    context=platform_context()
    service=PlatformService(context.store,context)
    user_id=os.environ['PADELWEB_USER_ID']
    service.require_writable(user_id)
    return context.store.user(user_id)


def initialise_key():
    from filelock import FileLock
    store = TokenStore(settings.padelbot_key_file,settings.padelbot_token_file)
    store.key_file.parent.mkdir(parents=True,exist_ok=True)
    with FileLock(str(store.key_file)+'.init.lock',timeout=15):
        if not store.key_file.exists():
            store.init_key()
    return store


def payment_summary(me: dict) -> dict:
    return {"balance":me.get("balance") or 0,
            "direct_debit":False,
            'stored_method':me.get('_stored_method',{}).get('type')==3,
            'stored_card':me.get('_stored_method',{}).get('type')==3,
            "manage_url":"https://usc.kuleuven.cloud/members/profile/myshopping#storedpaymethodcard"}


def verified_bookings(client, me):
    from .booking_manager import list_my_bookings
    from .booking import get_product_names
    bookings = list_my_bookings(client,member_id=int(me['id']))
    names=get_product_names(client,{int(b.raw['productId']) for b in bookings if b.raw.get('productId')})
    device_verified=settings.padelbot_android_profile_file.exists()
    # paidFor is also true on temporary, unpaid holds. Check the owning sale.
    completed={}
    for booking in bookings:
        if booking.sale_id and booking.sale_id not in completed:
            sale=client.get(f'/sales/{booking.sale_id}',params={'s':{'id':booking.sale_id,'customerPersonId':int(me['id'])}})
            completed[booking.sale_id]=(sale.get('id')==booking.sale_id and sale.get('customerPersonId')==int(me['id'])
                and type(sale.get('statusCode')) is int and sale['statusCode']==0)
    return {'device_verified':device_verified,'bookings':[
        {'id':b.id,'sale_id':b.sale_id,'description':b.description,'name':names.get(b.raw.get('productId'),'Reservatie'),
         'start':b.start_iso,'end':b.end_iso,'paid':b.paid_for,
         'confirmed':device_verified and b.status==0 and b.paid_for is True and completed.get(b.sale_id,False)
             and 'padel' in names.get(b.raw.get('productId'),'').lower()}
        for b in bookings]}


def platform_context():
    from .web_community import CommunityBookings
    from .web_store import PlatformStore
    with sqlite3.connect(os.environ['PADELWEB_DB']) as c:
        owner_email=c.execute('SELECT email FROM users WHERE owner=1').fetchone()[0]
    return CommunityBookings(PlatformStore(Path(os.environ['PADELWEB_DB']).parent,owner_email))


def execute(kind: str, payload: dict) -> dict:
    global _reservation_started,_prepared_delivery,_prepared_service
    _reservation_started=False
    _prepared_delivery=_prepared_service=None
    if kind in {'book','rule','draft_refresh'} and os.environ.get('PADELWEB_DB'):
        assert_writable_profile()
    from .client import BackboneClient
    from .booking import find_slots_window, find_slots, book, get_product_names
    from .booking_manager import list_my_bookings
    from .checkout import slot_price
    from .rules_store import RulesStore
    token_store=initialise_key()
    if kind == "login":
        from .android import AndroidApprover
        from .auth import login_via_android
        if not settings.padelbot_android_profile_file.exists():
            raise ValueError("Koppel eerst je eigen virtuele authenticator via Instellingen.")
        from filelock import FileLock
        with FileLock(str(token_store.blob_file)+'.renew.lock',timeout=600):
            tokens = login_via_android(AndroidApprover())
            token_store.save(tokens)
    with BackboneClient() as client:
        me = client.me()
        if kind in {'account','login','quote','book','rule'}:
            from .payment import attach_stored_method
            me=attach_stored_method(client,me,cards_only=True)
        if kind in {"account","login"}:
            return {"connected":True,"name":str(me.get("firstName") or me.get("name") or "KUL-account"),
                    "payment":payment_summary(me)}
        if kind == "slots":
            start,end = int(payload["start"]),int(payload["end"])
            if not 0<=start<=end<=30:
                raise ValueError("Kies een dag of periode tussen 0 en 30.")
            today = datetime.now(ZoneInfo("Europe/Brussels"))
            slots = find_slots_window(client,start_date=today+timedelta(days=start),days=end-start+1)
            names=get_product_names(client,{s.bookable_product_id for s in slots})
            return {"slots":[{"product":s.bookable_product_id,"court":s.bookable_linked_product_id,
                              "start":s.start_iso,"end":s.end_iso,"price":s.price,'name':names.get(s.bookable_product_id,'Padel')} for s in slots if s.start_dt>today]}
        if kind == "bookings":
            return verified_bookings(client,me)
        if kind in {'draft_refresh','draft_release'}:
            from .drafts import DraftStore,ensure_draft_cart,draft_deadline
            from .web_community import timestamp
            from filelock import FileLock
            import time
            drafts=DraftStore(platform_context())
            draft=drafts.owned(payload['draft_id'],os.environ['PADELWEB_USER_ID'])
            settings.padelbot_rules_file.parent.mkdir(parents=True,exist_ok=True)
            with FileLock(str(settings.padelbot_rules_file)+'.booking.lock',timeout=180):
                if kind=='draft_release':
                    drafts.release(client,draft['id'],int(me['id']))
                    return {'message':'Draft gestopt en eigen draft-item opgeruimd.'}
                if (draft['state'] not in {'pending','active','refreshing'} or time.time()>=min(draft['deadline'],draft_deadline(draft['created'],timestamp(draft['target']),time.time()))):
                    drafts.stop(draft['id'],draft['profile_id'],'expired')
                    drafts.release(client,draft['id'],int(me['id']))
                    raise ValueError('De maximale looptijd van deze draft is voorbij.')
                if not settings.padelbot_android_profile_file.exists():raise ValueError('Koppel eerst je eigen KUL-authenticator.')
                slots=find_slots(client,target_date=datetime.fromisoformat(draft['target']))
                slot=next((s for s in slots if s.start_dt==datetime.fromisoformat(draft['target']) and s.bookable_product_id==draft['product'] and s.bookable_linked_product_id==draft['court']),None)
                if not slot:
                    drafts.stop(draft['id'],draft['profile_id'],'unavailable')
                    drafts.release(client,draft['id'],int(me['id']))
                    raise ValueError('Dit terrein is niet meer beschikbaar. De draft is gestopt.')
                user=drafts.community.store.user(os.environ['PADELWEB_USER_ID'])
                if not user or user['suspended'] or user.get('restricted'):
                    drafts.stop(draft['id'],draft['profile_id'],'cancelled')
                    drafts.release(client,draft['id'],int(me['id']))
                    raise ValueError('Dit profiel kan geen nieuwe reserveringen maken; de draft is gestopt.')
                assert_writable_profile()
                item=ensure_draft_cart(client,slot,int(me['id']),int(me.get('category') or 0),'Sigmadvisor draft '+draft['id'])
                name=get_product_names(client,{slot.bookable_product_id}).get(slot.bookable_product_id,'Padel')
                drafts.activate(draft['id'],slot,item,name)
                return {'draft_id':draft['id'],'message':'Draft actief. Er is niets betaald en het terrein is nog niet definitief gereserveerd.'}
        if kind == 'community_join':
            community=platform_context()
            match=community.host(payload['match_id'])
            host=os.environ['PADELWEB_USER_ID']
            if match['user_id']!=host:
                raise ValueError('De organisator kon niet worden bevestigd.')
            actual=verified_bookings(client,me)
            community.sync(host,actual['bookings'],device_verified=actual['device_verified'])
            community.join_verified(match['id'],payload['participant_id'])
            return {'joined':True,'message':'Je neemt deel aan deze match.'}
        if kind not in {"book","rule","quote"}:
            raise ValueError("Onbekende taak.")
        prefs = current_preferences()
        if kind!="quote" and not prefs.get("automatic_payment") and (kind=='rule' or 'payment_mode' not in prefs):
            raise ValueError('Schakel autobooking in bij Instellingen.' if kind=='rule' else 'Kies eerst je betaalwijze bij Instellingen.')
        if kind=='rule':
            from .platform_service import PlatformService,payment_mode,validate_autobook_payment
            context=platform_context()
            PlatformService(context.store,context).remember_payment(os.environ['PADELWEB_USER_ID'],payment_summary(me))
            validate_autobook_payment(me,payment_mode(prefs))
        target = datetime.fromisoformat(payload["target"].replace('Z','+00:00'))
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
            slot = next((s for s in slots if s.start_dt==target and s.bookable_linked_product_id==int(payload["court"])
                         and s.bookable_product_id==int(payload['product'])),None)
        if slot is None:
            if kind=='rule':
                return {'waiting':True,'message':'Nog geen terrein vrij binnen je venster. We blijven controleren.'}
            raise ValueError("Dit tijdstip is niet meer vrij. Vernieuw de beschikbaarheid.")
        amount = slot_price(client,slot,me)
        if kind=="quote":
            return {"amount":str(amount),"product":slot.bookable_product_id,"court":slot.bookable_linked_product_id,"target":slot.start_iso,'end':slot.end_iso,
                    "payment":payment_summary(me)}
        maximum = Decimal(str(prefs.get("max_amount",0)))
        if (kind=='rule' or 'payment_mode' not in prefs) and (amount>maximum or maximum<=0):
            raise ValueError("De prijs ligt boven jouw ingestelde maximum. Er is niets geboekt.")
        # Only KUL balance and its verified tokenized credit-card path are used.
        # This worker never loads a legacy encrypted card or accepts browser card data.
        from .platform_service import payment_mode,validate_payment,PlatformService
        mode=payment_mode(prefs)
        validate_payment(me,amount,mode)
        if kind == "book" and amount != Decimal(str(payload["amount"])):
            raise ValueError("De prijs is gewijzigd. Vernieuw het overzicht en bevestig opnieuw.")
        context=platform_context();service=PlatformService(context.store,context)
        service.cipher()  # Verify durable private website storage before creating a sale.
        latest=current_preferences()
        if (payment_mode(latest)!=mode or
            ((kind=='rule' or 'payment_mode' not in latest) and (not latest.get('automatic_payment') or Decimal(str(latest.get('max_amount',0)))<amount))):
            raise ValueError('Je betaalinstellingen zijn gewijzigd. Er is niets geboekt.')
        if payload.get('draft_id'):
            from .drafts import DraftStore
            DraftStore(platform_context()).start_conversion(payload['draft_id'],os.environ['PADELWEB_USER_ID'])
        from .booking import _timestamp_key
        if any(_timestamp_key(b.start_iso)<_timestamp_key(slot.end_iso) and _timestamp_key(b.end_iso)>_timestamp_key(slot.start_iso)
            for b in list_my_bookings(client,member_id=int(me['id']))):
            raise ValueError('Je hebt al een boeking op dit tijdstip.')
        latest=current_preferences()
        if payment_mode(latest)!=mode or (kind=='rule' and (not latest.get('automatic_payment') or Decimal(str(latest.get('max_amount',0)))<amount)):
            raise ValueError('Je instellingen zijn gewijzigd. Er is niets geboekt.')
        if rule:
            current=rules.get_rule(rule.id)
            if not current or not current.enabled:raise ValueError('Autobooking is gestopt. Er is niets geboekt.')
            # A rule may wait for availability. Re-read the KUL-owned card token
            # immediately before the reservation, including for balance-funded sales.
            fresh=dict(me);fresh.pop('_stored_method',None)
            me=attach_stored_method(client,fresh,cards_only=True)
            service.remember_payment(os.environ['PADELWEB_USER_ID'],payment_summary(me))
            validate_autobook_payment(me,mode)
        assert_writable_profile()
        _reservation_started=True
        result = book(client,slot,member_id=int(me["id"]),me=me,expected_amount=amount,label=rule.notes if rule else "Sigmadvisor",payment_mode=mode)
        payment_via_discord=False
        if result.payment_url:
            payment_via_discord=service.queue_payment(os.environ['PADELWEB_USER_ID'],result.sale_id,result.payment_url,amount,slot.start_iso,booking_id=result.booking_id)
        if payload.get('draft_id') and result.booking_id:
            DraftStore(platform_context()).converted(payload['draft_id'],result.booking_id,result.paid)
        elif payload.get('draft_id'):
            DraftStore(platform_context()).uncertain_conversion(payload['draft_id'])
        rules.record(rule_id=rule.id if rule else None,success=result.paid,sale_id=result.sale_id,
            booking_id=result.booking_id,target_slot_iso=slot.start_dt.astimezone(ZoneInfo("Europe/Brussels")).isoformat(),
            error=None if result.paid else "Betaling nog niet bevestigd; controleer KU Leuven Sport.")
        return {"sale_id":result.sale_id,"booking_id":result.booking_id,"paid":result.paid,
                "amount":str(amount),"target":slot.start_iso,"payment_available":bool(result.payment_url),"payment_via_discord":payment_via_discord,
                "message":"Je hebt geboekt." if result.paid else "Betaal om je boeking te bevestigen." if result.payment_url else "Reservatie aangemaakt. Controleer de betaling in KU Leuven Sport."}


def main():
    # SIGTERM unwinds finally blocks, including emulator cleanup.
    signal.signal(signal.SIGTERM,lambda *_:sys.exit(1))
    message = json.loads(sys.stdin.buffer.read())
    from .auth import NativeAuthRequired
    try:
        result = execute(message["kind"],message["payload"])
    except NativeAuthRequired:
        # Only safe reads may leave their HTTP lane and resume after one
        # serialized login. Reservation/payment operations are never replayed.
        from .web_store import READ_JOB_KINDS
        result = {'reauth_required':True} if message['kind'] in READ_JOB_KINDS else {'error':'KUL vraagt opnieuw aanmelden. Controleer je koppeling.'}
    except (ValueError,) as error:
        result = {"error":str(error)}
    except BaseException:
        # Provider exceptions may contain account identifiers, challenge links or cookies.
        result = {"error":"KUL kon deze taak niet afronden. Controleer je koppeling, saldo en boekingen voor je opnieuw probeert."}
    if 'error' in result and message['kind'] in {'book','rule'} and not _reservation_started:
        result['safe_to_retry']=True
    if 'error' in result and _prepared_delivery:
        try:_prepared_service.close_prepared(os.environ['PADELWEB_USER_ID'],os.environ['PADELWEB_JOB_ID'],_prepared_delivery)
        except Exception:pass
    if 'error' in result and message['payload'].get('draft_id'):
        try:
            from .drafts import DraftStore
            drafts=DraftStore(platform_context())
            if message['kind']=='draft_refresh':drafts.failed(message['payload']['draft_id'],'KUL-controle niet bevestigd. Controleer de draft en je winkelwagen.')
            elif message['kind']=='book' and _reservation_started:drafts.uncertain_conversion(message['payload']['draft_id'])
        except Exception:pass
    Path(os.environ["PADELWEB_RESULT"]).write_text(json.dumps(result),encoding="utf-8")


if __name__ == "__main__":
    main()
