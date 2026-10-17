"""Headless Playwright driver for the Worldline hosted-checkout payment flow.

Flow:
  1. POST /sales/{id}/pay with payMethod=6 (already done by the caller).
  2. Use the hosted payment URL returned by the backend.
  3. THIS module: launch Chromium, follow the redirect to Worldline, fill the
     card form using stored card details, submit. If 3DS frictionless we land
     on the success callback. If 3DS challenges, DM the owner and wait.

We use multi-fallback selectors because Worldline's DOM isn't in our HARs.
Diagnostics record only field metadata, never screenshots or field values.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

from loguru import logger

from .card_store import Card, CardStore
from .config import settings


@dataclass
class WorldlineResult:
    success: bool
    error: str | None = None
    needed_3ds: bool = False
    final_url: str | None = None
    submitted: bool = False


# Selector candidates for Worldline's hosted-checkout fields. The first one
# that's visible on the page is used.
CARD_NUMBER_SELECTORS = [
    '#payment-cardnumber',
    'input[name="cardNumber"]',
    'input[name="CardNumber"]',
    'input[name="cardno"]',
    'input[autocomplete="cc-number"]',
    'input[id*="card-number" i]',
    'input[id*="cardnumber" i]',
    'input[placeholder*="card number" i]',
    'input[placeholder*="kaartnummer" i]',
]
EXPIRY_COMBINED_SELECTORS = [
    'input[name="cardExpiryDate"]',
    'input[name="ExpirationDate"]',
    'input[name="expiry"]',
    'input[autocomplete="cc-exp"]',
    'input[placeholder*="MM/YY" i]',
]
EXPIRY_MONTH_SELECTORS = [
    '#payment-cardexpirationmonth',
    'input[name="ExpirationMonth"]',
    'input[name="exp-month"]',
    'input[autocomplete="cc-exp-month"]',
    'select[name="ExpirationMonth"]',
]
EXPIRY_YEAR_SELECTORS = [
    '#payment-cardexpirationyear',
    'input[name="ExpirationYear"]',
    'input[name="exp-year"]',
    'input[autocomplete="cc-exp-year"]',
    'select[name="ExpirationYear"]',
]
CVC_SELECTORS = [
    '#payment-cvc',
    'input[name="cvc"]',
    'input[name="CVC"]',
    'input[name="cvv"]',
    'input[name="securityCode"]',
    'input[autocomplete="cc-csc"]',
    'input[placeholder*="CVC" i]',
    'input[placeholder*="CVV" i]',
]
HOLDER_SELECTORS = [
    '#payment-cardholdername',
    'input[name="CardholderName"]',
    'input[name="cardholder"]',
    'input[name="holderName"]',
    'input[autocomplete="cc-name"]',
    'input[placeholder*="cardholder" i]',
    'input[placeholder*="naam" i]',
]
SUBMIT_SELECTORS = [
    '#payment-submit-switch div[role="button"]:not([disabled="true"]):not([aria-disabled="true"])',
    '#payment-submit',
    '#payment-submit-switch-mobile',
]


def _detect_card_brand(card_number: str) -> str | None:
    """Map card number BIN → Worldline brand value used by the selection page.
    Returns one of BCMC_brand / Eurocard_brand / Maestro_brand / VISA_brand."""
    n = card_number.strip().replace(" ", "")
    if not n.isdigit():
        return None
    from .web_cards import card_network
    network,_=card_network(n)
    return {'visa':'VISA_brand','mastercard':'Eurocard_brand',
            'bancontact':'BCMC_brand','maestro':'Maestro_brand'}.get(network)


def _try_fill(page, selectors: list[str], value: str, *, kind: str) -> bool:
    for sel in selectors:
        try:
            el = page.locator(f"{sel}:visible").first
            if el.is_visible(timeout=2000):
                # If it's a select, use select_option; else fill
                tag = el.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    el.select_option(value)
                else:
                    el.click()
                    el.fill(value)
                logger.info("[worldline] filled {} via {}", kind, sel)
                return True
        except Exception as e:
            # Playwright's error text may contain the value passed to fill().
            logger.debug("[worldline] {} selector {} failed: {}", kind, sel, type(e).__name__)
    return False


def _try_click(page, selectors: list[str]) -> bool:
    for sel in selectors:
        try:
            el = page.locator(f"{sel}:visible").first
            visible = el.is_visible(timeout=2000)
        except Exception:
            continue
        if visible:
            # Once a click is attempted, its failure may follow a real submit.
            # Do not try another button and risk submitting a second time.
            try:
                el.click()
            except Exception:
                raise RuntimeError("Checkout submit outcome is unknown") from None
            logger.info("[worldline] clicked submit via {}", sel)
            return True
    return False


def _has_3ds_iframe(page) -> bool:
    for candidate in page.context.pages:
        for frame in candidate.frames:
            url = frame.url or ""
            try:
                host = urlsplit(url).hostname or ""
            except ValueError:
                continue
            if "acs" in host.lower() or "emv3ds" in url.lower() or "3ds" in url.lower():
                return True
    return False


def _checkout_field_errors(page) -> list[str]:
    """Return fixed field names; never expose validation text or card values."""
    if not _is_worldline_url(page.url):
        return []
    fields = {
        "payment-cardnumber-error": "kaartnummer",
        "payment-cardholdername-error": "kaarthouder",
        "payment-cardexpirationmonth-error": "vervaldatum",
        "payment-cvc-error": "CVC",
    }
    result = []
    for field_id, label in fields.items():
        error = page.locator(f"#{field_id}:visible").first
        if error.count() and (error.text_content() or "").strip():
            result.append(label)
    if page.locator('.proceed-to-payment-label-error-id:visible').count():
        result.append("formulier")
    return result


def _callback_result(url: str) -> bool | None:
    """Recognize only USC's exact callback; this is not settlement evidence."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != "usc.kuleuven.cloud"
                or parsed.port not in (None, 443) or parsed.username is not None
                or parsed.password is not None or parsed.path != "/sales/callback"
                or parsed.fragment):
            return None
        return parse_qs(parsed.query, keep_blank_values=True).get("status") == ["success"]
    except ValueError:
        return None


def _url_without_secrets(url: str) -> str:
    try:
        parsed = urlsplit(url)
        return f"{parsed.scheme}://{parsed.hostname or ''}"
    except ValueError:
        return "invalid URL"


def _is_worldline_url(url: str) -> bool:
    try:
        parsed = urlsplit(str(url))
        return (parsed.scheme == "https" and parsed.hostname == "payment.direct.worldline-solutions.com"
                and parsed.port in (None, 443) and parsed.username is None and parsed.password is None)
    except ValueError:
        return False


def _wait_checkout_controls(page, *, include_brands: bool = True) -> None:
    # Worldline can first load an intermediate page and navigate again after
    # networkidle. Wait for actual controls rather than inspecting that URL.
    selectors = CARD_NUMBER_SELECTORS + (["input[type=radio][name=selectionform]"] if include_brands else [])
    page.wait_for_function("""selectors => selectors.some(selector =>
        Array.from(document.querySelectorAll(selector)).some(el =>
            el.getClientRects().length > 0))""", arg=selectors, timeout=15_000)


def _advance_checkout(page) -> bool:
    """Follow observed Worldline continuation buttons; never operate bank UI."""
    if not _is_worldline_url(page.url):
        return False
    for selector in ['#payment-ContinueToPaymentPage', '#payment-ContinueToMerchantWebsite']:
        button = page.locator(f"{selector}:visible").first
        if button.is_visible() and button.is_enabled():
            button.click()
            return True
    return False


def pay_via_worldline(
    *,
    payment_url: str,
    card: Card,
    on_3ds_challenge=None,
    timeout_s: int = 120,
    headless: bool = True,
    diagnostics_dir: Path | None = None,
    allow_missing_cvv: bool = False,
) -> WorldlineResult:
    """Drive the Worldline hosted-checkout page with the saved card.

    Returns a WorldlineResult; success=True means we observed the success
    callback URL, NOT that the backend has confirmed payment. The caller must
    check the sale and payment state. Failure diagnostics contain field metadata.
    """
    from .card_store import validate_card
    try:
        validate_card(card,allow_missing_cvv=allow_missing_cvv)
    except ValueError:
        return WorldlineResult(success=False,error='De betaalkaart is ongeldig of vervallen.')
    try:
        from playwright.sync_api import sync_playwright, Error as PWError, TimeoutError as PWTimeout
    except ImportError as e:
        return WorldlineResult(success=False, error=f"playwright not installed: {e}")

    diagnostics_dir = diagnostics_dir or Path("data/worldline_diag")
    diagnostics_dir.mkdir(parents=True, exist_ok=True)

    needed_3ds = False
    final_url: str | None = None
    submit_attempted = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless,args=[
            '--disable-save-password-bubble',
            '--disable-features=AutofillServerCommunication,AutofillEnableAccountWalletStorage'])
        # Incognito, memory-only state: no profile, traces, HAR, video, saved
        # card autofill data or downloads containing payment details.
        ctx = browser.new_context(accept_downloads=False,service_workers='block')
        page = ctx.new_page()
        try:
            logger.info("[worldline] navigating to payment URL...")
            page.goto(payment_url, wait_until="domcontentloaded", timeout=30_000)

            # SAP redirect should send us to Worldline within a few seconds.
            try:
                page.wait_for_url(
                    _is_worldline_url,
                    timeout=20_000,
                )
            except PWTimeout:
                return WorldlineResult(success=False, error="Trusted Worldline checkout did not load")

            page.wait_for_load_state("networkidle", timeout=10_000)
            _wait_checkout_controls(page)
            if not _is_worldline_url(page.url):
                return WorldlineResult(success=False, error="Unexpected checkout controls origin")

            # Worldline shows a brand-selection page first when multiple
            # methods are configured. Pick the brand matching our card number,
            # then click the "Continue/Proceed" button to go to the form.
            if page.locator('input[type="radio"][name="selectionform"]').count():
                brand = _detect_card_brand(card.number)
                if not brand:
                    _save_diag(page, diagnostics_dir, "unknown-card-brand")
                    return WorldlineResult(success=False, error="could not infer card brand", final_url=page.url)
                logger.info("[worldline] selection page — picking brand={}", brand)
                try:
                    page.locator(f'input[type="radio"][value="{brand}"]').first.click(timeout=5_000)
                except PWError as e:
                    _save_diag(page, diagnostics_dir, f"brand-radio-{brand}")
                    return WorldlineResult(success=False, error=f"could not select {brand}: {type(e).__name__}", final_url=page.url)
                # Click "proceed to payment" — it un-disables once a radio is picked.
                # Desktop and mobile buttons share the class; the first one
                # can be hidden. Follow the visible button's normal handler.
                proceed = page.locator('button.proceed-to-payment-button-id:not(.disabled):visible').first
                proceed.wait_for(state="visible", timeout=10_000)
                proceed.click(timeout=10_000)
                # The card form may follow another intermediate navigation.
                _wait_checkout_controls(page, include_brands=False)

            if not _is_worldline_url(page.url):
                return WorldlineResult(success=False, error="Unexpected card form origin")

            # Inputs can appear before Worldline's validation bundle loads.
            # Filling then can start 3DS preparation with incomplete handlers
            # and leave the form stuck without sending its payment request.
            page.wait_for_load_state("load", timeout=15_000)
            page.wait_for_load_state("networkidle", timeout=15_000)

            # Fill the card form only on the observed Worldline host.
            number_ok = _try_fill(page, CARD_NUMBER_SELECTORS, card.number, kind="cardNumber")
            if not number_ok:
                _save_diag(page, diagnostics_dir, "no-card-number")
                return WorldlineResult(success=False, error="card number input not found", final_url=page.url)

            holder_ok = _try_fill(page, HOLDER_SELECTORS, card.holder, kind="cardholder")
            if not holder_ok:
                logger.warning("[worldline] cardholder field not found (may be optional)")

            mm = f"{card.exp_month:02d}"
            yy = str(card.exp_year)[-2:]
            yyyy = str(card.exp_year)
            combined_value = f"{mm}/{yy}"
            exp_ok = _try_fill(page, EXPIRY_COMBINED_SELECTORS, combined_value, kind="expiry")
            if not exp_ok:
                m_ok = _try_fill(page, EXPIRY_MONTH_SELECTORS, mm, kind="expiryMonth")
                y_ok = _try_fill(page, EXPIRY_YEAR_SELECTORS, yyyy, kind="expiryYear")
                if not y_ok:
                    y_ok = _try_fill(page, EXPIRY_YEAR_SELECTORS, yy, kind="expiryYearShort")
                exp_ok = m_ok and y_ok
            if not exp_ok:
                return WorldlineResult(success=False, error="Expiry fields could not be filled", final_url=page.url)

            if card.cvv:
                cvv_ok = _try_fill(page, CVC_SELECTORS, card.cvv, kind="cvv")
                if not cvv_ok:
                    _save_diag(page, diagnostics_dir, "no-cvv")
                    return WorldlineResult(success=False, error="CVV input not found", final_url=page.url)
            elif any(page.locator(f'{selector}:visible').count() for selector in CVC_SELECTORS):
                # A saved PAN never includes CVC. Do not guess, synthesize a
                # value or submit a form requesting it; use the private link.
                return WorldlineResult(success=False,error='Bevestig de CVC via je betaallink.',final_url=page.url)

            # Submit
            submit_attempted = True
            if not _try_click(page, SUBMIT_SELECTORS):
                submit_attempted = False
                _save_diag(page, diagnostics_dir, "no-submit")
                return WorldlineResult(success=False, error="submit button not found", final_url=page.url)

            # Wait for either: success callback URL, OR 3DS iframe to appear
            deadline = time.monotonic() + timeout_s
            poll = 0.5
            while time.monotonic() < deadline:
                try:
                    cur = page.url
                except Exception:
                    cur = ""
                if _callback_result(cur) is not None:
                    final_url = cur
                    logger.info("[worldline] reached USC payment callback")
                    break
                errors = _checkout_field_errors(page)
                if errors:
                    _save_diag(page, diagnostics_dir, "form-rejected")
                    return WorldlineResult(success=False,
                        error="Checkout form validation failed: " + ", ".join(errors),
                        final_url=page.url, submitted=submit_attempted)
                if _advance_checkout(page):
                    continue
                if not needed_3ds and _has_3ds_iframe(page):
                    needed_3ds = True
                    logger.info("[worldline] bank verification frame observed")
                    if allow_missing_cvv:
                        return WorldlineResult(success=False,error='Bevestig de betaling bij je bank via je betaallink.',
                            needed_3ds=True,final_url=page.url,submitted=submit_attempted)
                    if on_3ds_challenge:
                        try:
                            on_3ds_challenge()
                        except Exception as e:
                            logger.warning("[worldline] on_3ds_challenge raised: {}", e)
                try:
                    page.wait_for_timeout(int(poll * 1000))
                except PWError:
                    break

            if final_url is None:
                _save_diag(page, diagnostics_dir, "timeout-or-stuck")
                return WorldlineResult(
                    success=False,
                    error=f"timed out after {timeout_s}s waiting for callback",
                    needed_3ds=needed_3ds,
                    final_url=page.url,
                    submitted=submit_attempted,
                )

            success = _callback_result(final_url) is True
            return WorldlineResult(success=success, needed_3ds=needed_3ds, final_url=final_url, submitted=submit_attempted)

        except Exception as e:
            try:
                _save_diag(page, diagnostics_dir, "exception")
            except Exception:
                pass
            return WorldlineResult(success=False, error=f"Checkout browser failed: {type(e).__name__}", final_url=page.url if page else None, submitted=submit_attempted)
        finally:
            try:
                browser.close()
            except Exception:
                pass


def _save_diag(page, dirpath: Path, label: str) -> None:
    """Save field metadata only; the page may contain card or bank credentials."""
    from datetime import datetime
    import json
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    base = dirpath / f"{ts}_{label}"
    try:
        fields = page.evaluate("""() => Array.from(document.querySelectorAll('input,select,button'))
            .map(el => ({tag: el.tagName.toLowerCase(), type: el.getAttribute('type'),
                         name: el.getAttribute('name'), id: el.id,
                         autocomplete: el.getAttribute('autocomplete'),
                         visible: el.getClientRects().length > 0,
                         valid: el.validity ? el.validity.valid : null}))""")
        base.with_suffix(".json").write_text(json.dumps({
            "origin": _url_without_secrets(page.url), "fields": fields,
            "error_fields": _checkout_field_errors(page),
            "frame_origins": [_url_without_secrets(f.url) for p in page.context.pages for f in p.frames],
        }, indent=2), encoding="utf-8")
    except Exception:
        pass
    logger.warning("[worldline] saved field metadata to {}.json", base)


def load_card() -> Card | None:
    """Load card from .env first (if set), else from the encrypted store."""
    if (settings.kul_card_number and settings.kul_card_cvv
            and settings.kul_card_exp_month and settings.kul_card_exp_year):
        return Card(
            holder=settings.kul_card_holder or "Cardholder",
            number=settings.kul_card_number.replace(" ", ""),
            exp_month=int(settings.kul_card_exp_month),
            exp_year=int(settings.kul_card_exp_year),
            cvv=settings.kul_card_cvv,
        )
    store = CardStore(settings.padelbot_key_file, settings.padelbot_card_file)
    try:
        return store.load()
    except FileNotFoundError:
        return None
