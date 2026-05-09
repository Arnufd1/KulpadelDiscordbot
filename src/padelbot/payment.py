"""Payment methods for Delcom backbone-web-api.

Endpoint-only paths:
  - PAY_METHOD_ACCOUNT (2)        — works if balance > 0
  - PAY_METHOD_DIRECT_DEBIT (0)   — works if directDebitSpec > 0
  - PAY_METHOD_CREDIT_CARD (6)    — needs Worldline UI driver (worldline_driver.py)
"""
from __future__ import annotations
from enum import IntEnum
from typing import Any

from loguru import logger

from .booking import SALES_PERSON_ID, SITE_ID
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


def has_direct_debit_mandate(me: dict[str, Any]) -> bool:
    """Is this account set up for SEPA direct debit?"""
    return bool(me.get("directDebitMandate"))


def create_payment(
    client: BackboneClient,
    *,
    sale_id: int,
    member_id: int,
    amount: float,
    method: PayMethod,
) -> dict[str, Any]:
    """Create a payment record. For DIRECT_DEBIT this fully completes the
    payment (server collects on schedule). For CREDIT_CARD, the response
    contains a Worldline session that needs separate handling."""
    body = {
        "saleId": sale_id,
        "salesPersonId": SALES_PERSON_ID,
        "memberId": member_id,
        "payMethod": int(method),
        "collectStatus": COLLECT_STATUS_SCHEDULED,
        "amount": amount,
        "siteId": SITE_ID,
        "posId": None,
    }
    logger.info("POST /payments method={} amount={} EUR sale={}", method.name, amount, sale_id)
    return client.post("/payments", body)


def build_payment_url(payment_resp: dict[str, Any], me: dict[str, Any], amount: float) -> str | None:
    """Construct the KUL SAP-redirect URL that sends the user to Worldline.

    Reverse-engineered from the SPA's `ogoneFormComponent.startPayment` (case
    `type === "PAYMENT_URL"`). The URL:

      http://www.kuleuven.be/sapredir/kredietbetaling_Detail.html?
          ges_mededeling=400/0028/83666 (fixed KUL bank reference)
          amount=<eur>
          currency=EUR
          sub=X & nieuw=X & secure=X
          result_url=<callback host without scheme>
          omschrijving=<payment reference>
          bet_nm=<lastName> & bet_vn=<firstName> & bet_email=<email>

    User clicks → SAP redirect → Worldline hosted checkout (session id assigned
    on Worldline's side) → enters card → 3DS → callback.
    """
    if not isinstance(payment_resp, dict):
        return None
    reference = payment_resp.get("reference")
    if not reference:
        return None

    # Result URL: scheme stripped (matches the SPA's behavior)
    callback = "https://usc.kuleuven.cloud/sales/callback?status=success"
    callback_no_scheme = callback.replace("https://", "").replace("http://", "")

    params = [
        ("ges_mededeling", "400/0028/83666"),
        ("amount", str(amount)),
        ("currency", "EUR"),
        ("sub", "X"),
        ("nieuw", "X"),
        ("result_url", callback_no_scheme),
        ("secure", "X"),
        ("omschrijving", reference),
        ("bet_nm", me.get("lastName") or ""),
        ("bet_vn", me.get("firstName") or ""),
        ("bet_email", me.get("email") or "member_without_email@example.dom"),
    ]
    from urllib.parse import urlencode
    return "http://www.kuleuven.be/sapredir/kredietbetaling_Detail.html?" + urlencode(params)


def auto_pay(
    client: BackboneClient,
    *,
    sale_id: int,
    member_id: int,
    amount: float,
    me: dict[str, Any] | None = None,
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

    if has_direct_debit_mandate(me) and int(me.get("directDebitSpec") or 0) > 0:
        return PayMethod.DIRECT_DEBIT, create_payment(
            client, sale_id=sale_id, member_id=member_id,
            amount=amount, method=PayMethod.DIRECT_DEBIT,
        )

    # Last resort: credit card via Worldline. Server creates the payment intent
    # and (we expect) returns a checkout URL or session id. Caller should
    # surface the URL via Discord DM so user can click to complete payment.
    resp = create_payment(
        client, sale_id=sale_id, member_id=member_id,
        amount=amount, method=PayMethod.CREDIT_CARD,
    )
    return PayMethod.CREDIT_CARD, resp
