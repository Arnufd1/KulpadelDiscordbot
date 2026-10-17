from copy import deepcopy

import pytest

from padelbot import payment, worldline_driver


@pytest.mark.parametrize('result',['empty','failed','debit_only'])
def test_stored_card_refresh_never_retains_an_old_unverified_token(monkeypatch,result):
    original={'id':42,'_stored_method':{'id':10,'type':3,'preferred':True}}
    def methods(*args):
        if result=='failed':raise RuntimeError('Provider unavailable')
        return [{'id':11,'type':2,'preferred':True}] if result=='debit_only' else []
    monkeypatch.setattr(payment,'stored_methods',methods)
    refreshed=payment.attach_stored_method(object(),original,cards_only=True)
    assert '_stored_method' not in refreshed
    assert original['_stored_method']['id']==10


SALE = {
    "id": 12, "statusCode": 0, "total": "24.00",
    "payments": [{"saleId": 12, "amount": "24.00", "collectStatus": 0,
                  "reference": "12-payment"}],
}
CHECKOUT = {
    "sale": {"id": 12, "statusCode": 1, "total": 24},
    "createdPayment": {"saleId": 12, "amount": 24, "collectStatus": 2, "payMethod": 6, "reference": "12-payment"},
    "userInitiatedPaymentResult": {"bSuccess": True, "bPaidOnStoredPayMethod": False,
                                   "hostedPaymentPageUrl": "http://www.kuleuven.be/sapredir/kredietbetaling_Detail.html?amount=24&omschrijving=12-payment"},
}


@pytest.mark.parametrize("url, expected", [
    ("https://usc.kuleuven.cloud/sales/callback?status=success&paymentReference=12-payment", True),
    ("https://usc.kuleuven.cloud:443/sales/callback?status=success", True),
    ("https://usc.kuleuven.cloud/sales/callback?status=failure", False),
    ("https://usc.kuleuven.cloud/sales/callback?status=success&status=failure", False),
    ("https://usc.kuleuven.cloud/sales/callback?status=successful", False),
    ("https://usc.kuleuven.cloud/sales/callback", False),
    ("https://usc.kuleuven.cloud/overview", None),
    ("https://usc.kuleuven.cloud/sales/12/checkout?status=success", None),
    ("https://usc.kuleuven.cloud.attacker.example/sales/callback?status=success", None),
    ("https://attacker.example/?return=https://usc.kuleuven.cloud/sales/callback&status=success", None),
    ("https://usc.kuleuven.cloud@attacker.example/sales/callback?status=success", None),
    ("https://user@usc.kuleuven.cloud/sales/callback?status=success", None),
    ("http://usc.kuleuven.cloud/sales/callback?status=success", None),
    ("https://usc.kuleuven.cloud:444/sales/callback?status=success", None),
    ("https://usc.kuleuven.cloud/sales/callback?status=success#status=failure", None),
])
def test_callback_origin_and_status(url, expected):
    assert worldline_driver._callback_result(url) is expected


def confirmed(sale, *, require_collected=True):
    return payment._sale_payment_confirmed(
        sale, sale_id=12, amount=24, reference="12-payment",
        require_collected=require_collected,
    )


def test_complete_sale_with_collected_payment():
    assert confirmed(SALE)


@pytest.mark.parametrize("field,value", [
    ("id", 13), ("statusCode", 1), ("statusCode", 3), ("statusCode", False),
    ("statusCode", None), ("total", "25.00"), ("total", "NaN"), ("payments", []),
])
def test_incomplete_or_wrong_sale_is_not_payment_confirmation(field, value):
    sale = deepcopy(SALE)
    sale[field] = value
    assert not confirmed(sale)


@pytest.mark.parametrize("field,value", [
    ("saleId", 13), ("collectStatus", 2), ("collectStatus", 8),
    ("collectStatus", False), ("reference", "other-payment"),
    ("amount", "23.99"), ("amount", "NaN"), ("amount", "-24"),
])
def test_wrong_pending_cancelled_or_partial_payment(field, value):
    sale = deepcopy(SALE)
    sale["payments"][0][field] = value
    assert not confirmed(sale)


def test_direct_debit_requires_completed_sale_but_can_collect_later():
    sale = deepcopy(SALE)
    sale["payments"][0]["collectStatus"] = 2
    assert confirmed(sale, require_collected=False)
    sale["statusCode"] = 1
    assert not confirmed(sale, require_collected=False)


def test_confirmation_polls_without_any_payment_write(monkeypatch):
    responses = [dict(SALE, statusCode=1), SALE]
    calls = []
    class Client:
        def get(self, path, params):
            calls.append((path, params))
            return responses.pop(0)
        def post(self, *args, **kwargs):
            pytest.fail("Payment confirmation must never create/retry a payment")
    monkeypatch.setattr(payment.time, "sleep", lambda _: None)
    assert payment.wait_for_payment_confirmation(
        Client(), sale_id=12, member_id=42, amount=24, reference="12-payment",
    )
    assert len(calls) == 2
    assert calls[0] == ("/sales/12", {"s": {"id": 12, "customerPersonId": 42}, "join": "payments"})


def test_unknown_state_times_out_without_repeating_payment():
    class Client:
        def get(self, *args, **kwargs):
            return dict(SALE, statusCode=1)
    assert not payment.wait_for_payment_confirmation(
        Client(), sale_id=12, member_id=42, amount=24, timeout_s=0,
    )


def test_payment_diagnostics_never_capture_values_or_screenshots(tmp_path):
    class Page:
        url = "https://payments.example/form?card=secret&session=secret"
        @property
        def context(self):
            from types import SimpleNamespace
            return SimpleNamespace(pages=[SimpleNamespace(frames=[SimpleNamespace(url=self.url)])])
        def screenshot(self, **kwargs):
            pytest.fail("Payment screenshots can disclose card data")
        def content(self):
            pytest.fail("Payment HTML can disclose card data")
        def evaluate(self, code):
            assert '.value' not in code and 'innerText' not in code
            return [{"tag": "input", "name": "cardNumber"}]
    worldline_driver._save_diag(Page(), tmp_path, "no-submit")
    [diagnostic] = list(tmp_path.iterdir())
    contents = diagnostic.read_text(encoding="utf-8")
    assert diagnostic.suffix == ".json"
    assert "secret" not in contents
    assert "cardNumber" in contents


def test_playwright_fill_exception_does_not_log_card_value():
    logs = []
    from loguru import logger
    class Field:
        def is_visible(self, **kwargs): return True
        def evaluate(self, code): return "input"
        def click(self): pass
        def fill(self, value): raise RuntimeError(f"fill({value}) failed")
    class Locator:
        first = Field()
    class Page:
        def locator(self, selector): return Locator()
    sink = logger.add(lambda message: logs.append(str(message)), level="DEBUG")
    try:
        assert not worldline_driver._try_fill(Page(), ["input"], "sensitive-card", kind="card")
    finally:
        logger.remove(sink)
    assert all("sensitive-card" not in line for line in logs)


@pytest.mark.parametrize("completed", [False, True])
def test_booking_success_callback_still_requires_backend_confirmation(monkeypatch, completed):
    from padelbot import booking
    slot = booking.Slot(1, 2, "2026-10-09T10:00:00Z", "2026-10-09T11:00:00Z", 24, {})
    monkeypatch.setattr(booking, "add_to_cart", lambda *args, **kwargs: None)
    monkeypatch.setattr(booking, "create_sale", lambda *args, **kwargs: {"id": 12, "total": 24})
    monkeypatch.setattr(booking, "fetch_booking_id", lambda *args: 123)
    monkeypatch.setattr(payment, "auto_pay", lambda *args, **kwargs: (payment.PayMethod.CREDIT_CARD, deepcopy(CHECKOUT)))
    monkeypatch.setattr(worldline_driver, "load_card", lambda: object())
    monkeypatch.setattr(worldline_driver, "pay_via_worldline", lambda **kwargs: worldline_driver.WorldlineResult(success=True))
    original = payment.wait_for_payment_confirmation
    monkeypatch.setattr(payment, "wait_for_payment_confirmation", lambda *args, **kwargs: original(*args, **kwargs, timeout_s=0))
    class Client:
        def get(self, path, params):
            assert path == "/sales/12"
            return dict(SALE, statusCode=0 if completed else 1)
    result = booking.book(Client(), slot, member_id=42, me={})
    assert result.paid is completed
    assert result.payment_pending is not completed
    message = payment.payment_message(result)
    if completed:
        assert "Paid via" in message
    else:
        assert "before paying again" in message
        assert "Complete checkout" not in message


def test_network_failure_during_confirmation_preserves_uncertain_status(monkeypatch):
    from padelbot import booking
    slot = booking.Slot(1, 2, "2026-10-09T10:00:00Z", "2026-10-09T11:00:00Z", 24, {})
    monkeypatch.setattr(booking, "add_to_cart", lambda *args, **kwargs: None)
    monkeypatch.setattr(booking, "create_sale", lambda *args, **kwargs: {"id": 12, "total": 24})
    monkeypatch.setattr(booking, "fetch_booking_id", lambda *args: 123)
    monkeypatch.setattr(payment, "auto_pay", lambda *args, **kwargs: (payment.PayMethod.ACCOUNT, {}))
    class Client:
        def get(self, *args, **kwargs): raise ConnectionError("network unavailable")
    result = booking.book(Client(), slot, member_id=42, me={})
    assert not result.paid and result.payment_pending
    assert "before paying again" in payment.payment_message(result)


def test_checkout_replaces_undecided_placeholder_using_captured_api_contract():
    calls = []
    class Client:
        def get(self, path, params):
            return {"id": 12, "customerPersonId": 42, "statusCode": 1, "total": 24,
                    "payments": [{"collectStatus": 2, "payMethod": 12}]}
        def post(self, path, body):
            calls.append((path, body))
            return deepcopy(CHECKOUT)
    response = payment.create_payment(Client(), sale_id=12, member_id=42, amount=24,
                                      method=payment.PayMethod.CREDIT_CARD)
    assert calls == [("/sales/12/pay", {
        "storedPayMethodId": None, "payMethod": 6,
        "onSuccessRedirectUrl": "https://usc.kuleuven.cloud/sales/callback",
        "createStoredPayMethod": False, "paymentProviderType": 8,
    })]
    assert payment.payment_reference(response) == "12-payment"
    assert payment.build_payment_url(response).startswith("https://www.kuleuven.be/sapredir/")


@pytest.mark.parametrize("update", [
    {"customerPersonId": 43}, {"id": 13}, {"total": 25}, {"total": "NaN"},
    {"statusCode": 0}, {"statusCode": 2}, {"statusCode": False},
    {"payments": [{"collectStatus": 2, "payMethod": 6}]},
])
def test_checkout_never_posts_for_wrong_amount_owner_or_existing_payment(update):
    class Client:
        def get(self, *args, **kwargs):
            return dict({"id": 12, "customerPersonId": 42, "statusCode": 1, "total": 24, "payments": []}, **update)
        def post(self, *args, **kwargs): pytest.fail("Preflight must prevent this payment")
    with pytest.raises(RuntimeError):
        payment.create_payment(Client(), sale_id=12, member_id=42, amount=24, method=payment.PayMethod.CREDIT_CARD)


def test_unknown_checkout_write_is_not_retried():
    import httpx
    calls = []
    class Client:
        def get(self, *args, **kwargs):
            return {"id": 12, "customerPersonId": 42, "statusCode": 1, "total": 24, "payments": []}
        def post(self, *args):
            calls.append(args)
            raise httpx.ReadTimeout("unknown result")
    with pytest.raises(payment.PaymentStateUnknownError):
        payment.create_payment(Client(), sale_id=12, member_id=42, amount=24, method=payment.PayMethod.CREDIT_CARD)
    assert len(calls) == 1


@pytest.mark.parametrize("url", [
    "https://attacker.example/hostedcheckout", "https://www.kuleuven.be.attacker.example/sapredir/kredietbetaling_Detail.html",
    "https://user@www.kuleuven.be/sapredir/kredietbetaling_Detail.html",
    "https://www.kuleuven.be/other-path", "http://payment.direct.worldline-solutions.com/form",
])
def test_checkout_rejects_unexpected_origins(url):
    response = deepcopy(CHECKOUT)
    response["userInitiatedPaymentResult"]["hostedPaymentPageUrl"] = url
    with pytest.raises(RuntimeError): payment.build_payment_url(response)
    assert not worldline_driver._is_worldline_url(url)


def test_payment_submit_is_not_repeated_after_uncertain_click():
    calls = []
    class Field:
        def is_visible(self, **kwargs): return True
        def click(self):
            calls.append("click")
            raise TimeoutError("Navigation may have followed a real payment submission")
    class Page:
        def locator(self, selector): return type("Locator", (), {"first": Field()})()
    with pytest.raises(RuntimeError, match="unknown"):
        worldline_driver._try_click(Page(), ["#payment-submit", "input[type=submit]"])
    assert calls == ["click"]


def test_worldline_continuation_never_clicks_bank_or_unexpected_pages():
    class Page:
        url = "https://acs.revolut.com/challenge"
        def locator(self, selector): pytest.fail("Bank UI must not be operated")
    assert not worldline_driver._advance_checkout(Page())
