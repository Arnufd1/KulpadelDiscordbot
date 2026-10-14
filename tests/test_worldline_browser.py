"""Exercise observed checkout controls against synthetic pages, with no network."""
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit

import pytest

from padelbot.card_store import Card
from padelbot.worldline_driver import pay_via_worldline, _try_click, SUBMIT_SELECTORS


@pytest.mark.parametrize("failure", [None, "missing_expiry", "frontend_rejection"])
@pytest.mark.parametrize("delayed_selection", [False, True])
@pytest.mark.parametrize("delayed_form_scripts", [False, True])
@pytest.mark.parametrize("brand,number", [("VISA_brand", "4242424242424242"), ("Eurocard_brand", "5555555555554444")])
def test_observed_worldline_form_and_merchant_return_without_real_payment(tmp_path, monkeypatch, failure, delayed_selection, delayed_form_scripts, brand, number):
    playwright = pytest.importorskip("playwright.sync_api")
    original_sync = playwright.sync_playwright
    requests = []
    submitted_fields = []
    worldline = "https://payment.direct.worldline-solutions.com"
    callback = "https://usc.kuleuven.cloud/sales/callback?status=success"

    def route_request(route):
        request = route.request
        url = urlsplit(request.url)
        requests.append((url.hostname, url.path))
        if url.hostname == "www.kuleuven.be":
            # A new navigation is intercepted separately. Playwright routes an
            # HTTP redirect chain only once, so avoid 302s in offline fixtures.
            landing = "/Landing" if delayed_selection else "/PaymentMethods/Selection"
            route.fulfill(content_type="text/html", body=f'<script>location.href="{worldline}{landing}"</script>')
            return
        if url.hostname == "usc.kuleuven.cloud" and url.path == "/sales/callback":
            route.fulfill(content_type="text/html", body="Checkout returned")
            return
        if url.hostname != "payment.direct.worldline-solutions.com":
            route.abort()
            return
        if url.path == "/Landing":
            # networkidle occurs before this second navigation. The initial
            # URL therefore cannot decide whether brand selection is needed.
            html = f'<script>setTimeout(() => location.href="{worldline}/hostedcheckout/PaymentMethods/Selection/session", 750)</script>'
        elif url.path in ("/PaymentMethods/Selection", "/hostedcheckout/PaymentMethods/Selection/session"):
            html = f'''<form action="{worldline}/Payment/Form" method="post">
                <input type="radio" value="{brand}" name="selectionform">
                <button type="submit" class="proceed-to-payment-button-id" style="display:none">Continue mobile</button>
                <button type="submit" class="proceed-to-payment-button-id">Continue</button></form>'''
        elif url.path == "/Payment/Form":
            expiry = '' if failure == "missing_expiry" else '''
                <select id="payment-cardexpirationmonth" name="cardexpirationmonth" autocomplete="cc-exp-month"><option value="10">10</option></select>
                <select id="payment-cardexpirationyear" name="cardexpirationyear" autocomplete="cc-exp-year"><option value="2030">2030</option></select>'''
            handler = '''onsubmit="event.preventDefault();document.getElementById('payment-cardnumber-error').textContent='Card validation failed';return false"''' if failure == "frontend_rejection" else ''
            html = f'''<form action="{worldline}/Payment/Submit" method="post" {handler}>
                <input type="submit" name="cancel" value="Cancel">
                <input id="payment-cardnumber" name="cardnumber" autocomplete="cc-number">
                <input id="payment-cardholdername" name="cardholdername" autocomplete="cc-name">
                <p id="payment-cardnumber-error" class="payment-error"></p>
                {expiry}<input id="payment-cvc" name="cvc" autocomplete="cc-csc">
                <input type="submit" id="payment-submit" name="submit" value="Pay"></form>'''
            if delayed_form_scripts:
                html = html.replace('id="payment-submit"', 'id="payment-submit" style="display:none"')
                html += '''<div id="payment-submit-switch">
                    <div role="button" onclick="document.forms[0].requestSubmit(document.getElementById('payment-submit'))">Pay</div>
                    </div>'''
                # Card fields are visible while a later bundle is loading.
                # A premature click stalls the form without a payment POST.
                html += '''<script>
                    window.checkoutReady = false;
                    document.forms[0].addEventListener('submit', event => {
                        if (!window.checkoutReady) event.preventDefault();
                    });
                    setTimeout(() => {
                        const script = document.createElement('script');
                        script.src = '/Validation.js';
                        document.head.appendChild(script);
                    }, 200);
                    </script>'''
        elif url.path == "/Validation.js":
            route.fulfill(content_type="application/javascript", body="window.checkoutReady = true;")
            return
        elif url.path == "/Payment/Submit":
            submitted_fields.append(parse_qs(request.post_data or '', keep_blank_values=True))
            html = f'''<form action="{worldline}/Redirect/Return">
                <input type="submit" id="payment-ContinueToPaymentPage" style="display:none" value="Continue mobile">
                <input type="submit" id="payment-ContinueToPaymentPage" value="Continue"></form>'''
        elif url.path == "/Redirect/Return":
            html = f'''<form action="{callback}">
                <input type="hidden" name="status" value="success">
                <input type="submit" id="payment-ContinueToMerchantWebsite" style="display:none" value="Return mobile">
                <input type="submit" id="payment-ContinueToMerchantWebsite" value="Return"></form>'''
        else:
            route.abort()
            return
        route.fulfill(content_type="text/html", body=html)

    @contextmanager
    def offline_playwright():
        with original_sync() as p:
            original_launch = p.chromium.launch
            def launch(**kwargs):
                try:
                    browser = original_launch(**kwargs)
                except playwright.Error as error:
                    if "Executable doesn't exist" in str(error):
                        pytest.skip("Install Chromium with python -m playwright install chromium")
                    raise
                original_context = browser.new_context
                def new_context(**kwargs):
                    context = original_context(**kwargs)
                    context.route("**/*", route_request)
                    return context
                monkeypatch.setattr(browser, "new_context", new_context)
                return browser
            monkeypatch.setattr(p.chromium, "launch", launch)
            yield p
    monkeypatch.setattr(playwright, "sync_playwright", offline_playwright)
    result = pay_via_worldline(
        payment_url="https://www.kuleuven.be/sapredir/kredietbetaling_Detail.html?amount=24",
        card=Card("Test", number, 10, 2030, "123"),
        timeout_s=5, diagnostics_dir=tmp_path,
    )
    if failure == "missing_expiry":
        assert not result.success and not result.submitted
        assert not submitted_fields
        assert "Expiry" in result.error
    elif failure == "frontend_rejection":
        assert not result.success and result.submitted
        assert not submitted_fields
        assert "kaartnummer" in result.error
        assert number not in result.error
    else:
        assert result.success and result.submitted
        assert len(submitted_fields) == 1
        assert submitted_fields[0]["submit"] == ["Pay"]
        assert "cancel" not in submitted_fields[0]
        assert submitted_fields[0]["cardexpirationmonth"] == ["10"]
        assert submitted_fields[0]["cardexpirationyear"] == ["2030"]
        assert result.final_url == callback


def test_cancel_and_confirm_buttons_are_never_used_as_payment_fallbacks():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except playwright.Error as error:
            if "Executable doesn't exist" in str(error):
                pytest.skip("Install Chromium with python -m playwright install chromium")
            raise
        page = browser.new_page()
        page.set_content('''<script>window.cancelClicks=0;</script>
            <input type="submit" id="payment-submit" style="display:none" value="Pay">
            <input type="submit" name="cancel" id="payment-cancel" value="Cancel"
                onclick="window.cancelClicks++;event.preventDefault();">
            <button type="submit" onclick="window.cancelClicks++;">Confirm cancellation</button>''')
        assert not _try_click(page, SUBMIT_SELECTORS)
        assert page.evaluate("window.cancelClicks") == 0
        browser.close()
