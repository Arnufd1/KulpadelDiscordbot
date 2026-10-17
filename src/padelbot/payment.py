"""USC sale checkout, matched to the captured /sales/{id}/pay flow."""
from __future__ import annotations
from enum import IntEnum
from decimal import Decimal, InvalidOperation
import time
import threading
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from loguru import logger

from .client import BackboneClient


class PayMethod(IntEnum):
    DIRECT_DEBIT = 0
    CASH = 1
    ACCOUNT = 2          # saldo / pre-paid balance
    BANK_CARD = 3
    INVOICE = 4
    CREDIT_CARD = 6      # via Worldline (HAR-confirmed)


# collectStatus 2 = "scheduled / to be collected" (HAR-confirmed for credit card path)
COLLECT_STATUS_SCHEDULED = 2
# Observed in USC's public frontend on 2026-10-08.
COLLECT_STATUS_COLLECTED = 0
SALE_STATUS_DONE = 0
PAYMENT_PROVIDER_OGONE = 8
_payment_lock = threading.Lock()


class PaymentStateUnknownError(RuntimeError):
    """A payment was requested; check server state before trying again."""


def has_pending_payment(sale: dict) -> bool:
    # A fresh sale has an UNDECIDED (12) placeholder, which /pay replaces.
    return any(isinstance(p, dict) and p.get("collectStatus") == COLLECT_STATUS_SCHEDULED
               and p.get("payMethod") not in (11, 12, 255, None)
               for p in sale.get("payments", []))


def payment_reference(response: dict) -> str | None:
    created = response.get("createdPayment") or {}
    reference = created.get("reference")
    return reference if isinstance(reference, str) and reference else None


def has_direct_debit_mandate(me: dict[str, Any]) -> bool:
    """Is this account set up for SEPA direct debit?"""
    return bool(me.get("directDebitMandate"))


def stored_methods(client,member_id):
    """USC's own person-scoped tokenized methods; never PAN or CVC."""
    from datetime import datetime,timezone
    out=client.get('/stored-pay-methods',params={'s':{'personId':member_id},'limit':100})
    data=out.get('data',[]) if isinstance(out,dict) else out
    valid=[];now=datetime.now(timezone.utc)
    for value in data if isinstance(data,list) else []:
        if (not isinstance(value,dict) or value.get('personId')!=member_id or type(value.get('id')) is not int
            or value['id']<=0 or value.get('type') not in {2,3} or value.get('paymentProviderType')!=PAYMENT_PROVIDER_OGONE):continue
        if value.get('expirationDate'):
            try:
                expiry=datetime.fromisoformat(value['expirationDate'].replace('Z','+00:00'))
                if (expiry.year,expiry.month)<=(now.year,now.month):continue
            except (ValueError,TypeError):continue
        valid.append({'id':value['id'],'type':value['type'],'paymentProviderType':PAYMENT_PROVIDER_OGONE,'preferred':value.get('preferred') is True})
    return valid


def attach_stored_method(client,me,*,cards_only=False):
    me=dict(me)
    # A failed or empty refresh must revoke any token from an older snapshot.
    me.pop('_stored_method',None)
    try:
        methods=stored_methods(client,int(me['id']))
        if cards_only:methods=[m for m in methods if m['type']==3]
        preferred=next((m for m in methods if m['preferred']),None)
        if preferred or len(methods)==1:me['_stored_method']=preferred or methods[0]
    except Exception:pass  # Unavailable/unverified methods cannot authorise a payment.
    return me


def create_payment(
    client: BackboneClient,
    *,
    sale_id: int,
    member_id: int,
    amount: float,
    method: PayMethod,
    stored_method_id: int | None = None,
) -> dict[str, Any]:
    """Start checkout once. The server supplies its own hosted payment URL.

    Check ownership, amount and existing payments before requesting checkout.
    Never retry a write when its outcome is unknown.
    """
    with _payment_lock:
        if stored_method_id is not None:
            selected=next((m for m in stored_methods(client,member_id) if m['id']==stored_method_id),None)
            if not selected or method!=(PayMethod.CREDIT_CARD if selected['type']==3 else PayMethod.DIRECT_DEBIT):
                raise RuntimeError('The linked KUL payment method is no longer available; no payment requested')
        sale = client.get(f"/sales/{sale_id}", params={
            "s": {"id": sale_id, "customerPersonId": member_id}, "join": "payments",
        })
        if (not isinstance(sale, dict) or sale.get("id") != sale_id
                or sale.get("customerPersonId") != member_id):
            raise RuntimeError("Sale ownership could not be confirmed")
        try:
            expected = Decimal(str(amount))
            total = Decimal(str(sale.get("total")))
            valid_amount = expected.is_finite() and expected > 0 and total == expected
        except (InvalidOperation, ValueError, TypeError):
            valid_amount = False
        if not valid_amount:
            raise RuntimeError("Sale amount changed or is invalid; no payment requested")
        if type(sale.get("statusCode")) is not int or sale["statusCode"] not in (1, 3):
            raise RuntimeError("Sale is already complete, cancelled, or unavailable")
        if has_pending_payment(sale):
            raise PaymentStateUnknownError("An existing payment is pending; check this sale before paying again")
        body = {
            "storedPayMethodId": stored_method_id,
            "payMethod": int(method),
            "onSuccessRedirectUrl": "https://usc.kuleuven.cloud/sales/callback",
            "createStoredPayMethod": False,
            "paymentProviderType": PAYMENT_PROVIDER_OGONE,
        }
        logger.info("POST /sales/{}/pay method={} amount={} EUR", sale_id, method.name, amount)
        try:
            response = client.post(f"/sales/{sale_id}/pay", body)
        except httpx.TransportError:
            raise PaymentStateUnknownError("Payment request outcome is unknown; check this sale before paying again") from None
        returned_sale = response.get("sale") if isinstance(response, dict) else None
        if not isinstance(returned_sale, dict) or returned_sale.get("id") != sale_id:
            raise PaymentStateUnknownError("Payment response was incomplete; check this sale before paying again")
        adapter = response.get("userInitiatedPaymentResult")
        if isinstance(adapter, dict) and adapter.get("bSuccess") is not True:
            raise RuntimeError("The payment provider rejected the checkout request")
        return response


def _sale_payment_confirmed(
    sale: Any, *, sale_id: int, amount: float, reference: str | None,
    require_collected: bool,
) -> bool:
    """Check backend state, including the expected sale, amount and payment."""
    if (not isinstance(sale, dict) or sale.get("id") != sale_id
            or type(sale.get("statusCode")) is not int
            or sale["statusCode"] != SALE_STATUS_DONE):
        return False
    try:
        expected = Decimal(str(amount))
        total = Decimal(str(sale.get("total")))
        if not expected.is_finite() or expected <= 0 or total != expected:
            return False
        # A completed direct-debit sale can be accepted before bank collection.
        if not require_collected:
            return True
        payments = sale.get("payments")
        if not isinstance(payments, list):
            return False
        collected = [p for p in payments if isinstance(p, dict)
                     and p.get("saleId") == sale_id
                     and type(p.get("collectStatus")) is int
                     and p["collectStatus"] == COLLECT_STATUS_COLLECTED]
        if reference and not any(p.get("reference") == reference for p in collected):
            return False
        amounts = [Decimal(str(p.get("amount"))) for p in collected]
        if any(not a.is_finite() or a < 0 for a in amounts):
            return False
        return sum(amounts, Decimal(0)) >= expected
    except (InvalidOperation, ValueError, TypeError):
        return False


def wait_for_payment_confirmation(
    client: BackboneClient, *, sale_id: int, member_id: int, amount: float,
    reference: str | None = None, require_collected: bool = True,
    timeout_s: float = 20,
) -> bool:
    """Poll only reads; never retry creating a payment after an uncertain result."""
    deadline = time.monotonic() + max(0, timeout_s)
    while True:
        sale = client.get(f"/sales/{sale_id}", params={
            "s": {"id": sale_id, "customerPersonId": member_id},
            "join": "payments",
        })
        if _sale_payment_confirmed(sale, sale_id=sale_id, amount=amount,
                                   reference=reference, require_collected=require_collected):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(2, remaining))


def payment_message(result) -> str:
    """Describe confirmed, uncertain, or unstarted checkout consistently."""
    if result.paid:
        return f":white_check_mark: Paid via **{result.pay_method}**."
    if result.payment_pending:
        return ":hourglass: Payment confirmation is pending. Check this sale in the KU Leuven Sport app before paying again."
    if result.payment_url:
        return f":credit_card: Complete checkout (EUR {result.amount}):\n{result.payment_url}"
    return f":warning: Payment not completed: `{result.pay_error or 'manual checkout needed'}`. Check this sale in the KU Leuven Sport app."


def build_payment_url(payment_resp: dict[str, Any]) -> str | None:
    """Use the backend checkout URL; never fabricate bank references or callbacks."""
    adapter = payment_resp.get("userInitiatedPaymentResult") if isinstance(payment_resp, dict) else None
    if not isinstance(adapter, dict) or adapter.get("bSuccess") is not True:
        return None
    url = adapter.get("hostedPaymentPageUrl")
    if not isinstance(url, str) or not url:
        return None
    try:
        parsed = urlsplit(url)
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError
        if parsed.hostname == "www.kuleuven.be" and parsed.path == "/sapredir/kredietbetaling_Detail.html":
            if parsed.scheme not in ("http", "https") or parsed.port not in (None, 80, 443):
                raise ValueError
            # The backend still emits HTTP. Use HTTPS for customer/payment data.
            return urlunsplit(("https", "www.kuleuven.be", parsed.path, parsed.query, ""))
        if (parsed.scheme == "https" and parsed.hostname == "payment.direct.worldline-solutions.com"
                and parsed.port in (None, 443)):
            return url
    except ValueError:
        pass
    raise RuntimeError("Checkout returned an unexpected payment origin")


def auto_pay(
    client: BackboneClient,
    *,
    sale_id: int,
    member_id: int,
    amount: float,
    me: dict[str, Any] | None = None,
    allow_direct_debit: bool = True,
) -> tuple[PayMethod, dict[str, Any]]:
    """Pick the best payment path and execute it.

    Order of preference:
      1. ACCOUNT (saldo) — if balance >= amount. Fully autonomous.
      2. DIRECT_DEBIT — if `directDebitSpec` > 0. Fully autonomous.
      3. CREDIT_CARD — creates a Worldline payment intent. Response contains
         a checkout URL the user clicks to finish payment. NOT autonomous, but
         removes the "find sale, click pay" friction — bot DMs the link.
    """
    if me is None:
        me = client.me()

    balance = float(me.get("balance") or 0)
    if balance >= amount:
        return PayMethod.ACCOUNT, create_payment(
            client, sale_id=sale_id, member_id=member_id,
            amount=amount, method=PayMethod.ACCOUNT,
        )

    if allow_direct_debit and has_direct_debit_mandate(me) and int(me.get("directDebitSpec") or 0) > 0:
        return PayMethod.DIRECT_DEBIT, create_payment(
            client, sale_id=sale_id, member_id=member_id,
            amount=amount, method=PayMethod.DIRECT_DEBIT,
        )

    stored=me.get('_stored_method')
    if stored and (stored['type']==3 or allow_direct_debit):
        method=PayMethod.CREDIT_CARD if stored['type']==3 else PayMethod.DIRECT_DEBIT
        return method,create_payment(client,sale_id=sale_id,member_id=member_id,amount=amount,
            method=method,stored_method_id=stored['id'])

    # Last resort: credit card via Worldline. Server creates the payment intent
    # and (we expect) returns a checkout URL or session id. Caller should
    # surface the URL via Discord DM so user can click to complete payment.
    resp = create_payment(
        client, sale_id=sale_id, member_id=member_id,
        amount=amount, method=PayMethod.CREDIT_CARD,
    )
    return PayMethod.CREDIT_CARD, resp
