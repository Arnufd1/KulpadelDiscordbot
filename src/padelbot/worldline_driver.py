"""Headless Playwright driver for the Worldline hosted-checkout payment flow.

Flow:
  1. POST /payments with payMethod=6 (already done by the caller) → reference.
  2. Construct SAP redirect URL via payment.build_payment_url() (already done).
  3. THIS module: launch Chromium, follow the redirect to Worldline, fill the
     card form using stored card details, submit. If 3DS frictionless we land
     on the success callback. If 3DS challenges, DM the owner and wait.

We use multi-fallback selectors because Worldline's DOM isn't in our HARs
(cross-origin response bodies are stripped by Chrome). On failure we save a
screenshot + page HTML so we can adjust selectors.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from loguru import logger

from .card_store import Card, CardStore
from .config import settings


@dataclass
class WorldlineResult:
    success: bool
    error: str | None = None
    needed_3ds: bool = False
    final_url: str | None = None


# Selector candidates for Worldline's hosted-checkout fields. The first one
# that's visible on the page is used.
CARD_NUMBER_SELECTORS = [
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
    'input[name="ExpirationMonth"]',
    'input[name="exp-month"]',
    'input[autocomplete="cc-exp-month"]',
    'select[name="ExpirationMonth"]',
]
EXPIRY_YEAR_SELECTORS = [
    'input[name="ExpirationYear"]',
    'input[name="exp-year"]',
    'input[autocomplete="cc-exp-year"]',
    'select[name="ExpirationYear"]',
]
CVC_SELECTORS = [
    'input[name="cvc"]',
    'input[name="CVC"]',
    'input[name="cvv"]',
    'input[name="securityCode"]',
    'input[autocomplete="cc-csc"]',
    'input[placeholder*="CVC" i]',
    'input[placeholder*="CVV" i]',
]
HOLDER_SELECTORS = [
    'input[name="CardholderName"]',
    'input[name="cardholder"]',
    'input[name="holderName"]',
    'input[autocomplete="cc-name"]',
    'input[placeholder*="cardholder" i]',
    'input[placeholder*="naam" i]',
]
SUBMIT_SELECTORS = [
    'button:has-text("Pay")',
    'button:has-text("Betalen")',
    'button:has-text("Betaal")',
    'input[type="submit"]',
    'button[type="submit"]',
    'button:has-text("Confirm")',
]


def _detect_card_brand(card_number: str) -> str | None:
    """Map card number BIN → Worldline brand value used by the selection page.
    Returns one of BCMC_brand / Eurocard_brand / Maestro_brand / VISA_brand."""
    n = card_number.strip().replace(" ", "")
    if not n.isdigit():
        return None
    if n.startswith("4"):
        return "VISA_brand"
    # Mastercard ranges: 51-55 and 2221-2720
    if n[:2] in {"51", "52", "53", "54", "55"}:
        return "Eurocard_brand"
    if n[:4].isdigit() and 2221 <= int(n[:4]) <= 2720:
        return "Eurocard_brand"
    # Bancontact (Belgian)
    if n.startswith("6703"):
        return "BCMC_brand"
    # Maestro: 6759 / 5018 / 5020 / 5038 / 6304 / 6759 / 676...
    if n[:4] in {"6759", "5018", "5020", "5038", "6304"}:
        return "Maestro_brand"
    return None


def _try_fill(page, selectors: list[str], value: str, *, kind: str) -> bool:
    for sel in selectors:
        try:
            el = page.locator(sel).first
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
            logger.debug("[worldline] {} selector {} failed: {}", kind, sel, e)
    return False


def _try_click(page, selectors: list[str]) -> bool:
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=2000):
                el.click()
                logger.info("[worldline] clicked submit via {}", sel)
                return True
        except Exception:
            continue
    return False


def _has_3ds_iframe(page) -> bool:
    for f in page.frames:
        url = f.url or ""
        if "acs.revolut" in url or "emv3ds" in url or "3ds" in url.lower():
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
) -> WorldlineResult:
    """Drive the Worldline hosted-checkout page with the saved card.

    Returns a WorldlineResult; success=True means we observed the success
    callback URL. On failure, a screenshot + HTML are saved to diagnostics_dir
    so we can iterate on selectors.
    """
    try:
        from playwright.sync_api import sync_playwright, Error as PWError, TimeoutError as PWTimeout
    except ImportError as e:
        return WorldlineResult(success=False, error=f"playwright not installed: {e}")

    diagnostics_dir = diagnostics_dir or Path("data/worldline_diag")
    diagnostics_dir.mkdir(parents=True, exist_ok=True)

    needed_3ds = False
    final_url: str | None = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context()
        page = ctx.new_page()
        try:
            logger.info("[worldline] navigating to payment URL...")
            page.goto(payment_url, wait_until="domcontentloaded", timeout=30_000)

            # SAP redirect should send us to Worldline within a few seconds.
            try:
                page.wait_for_url(
                    lambda u: ("worldline-solutions.com" in u) or ("hostedcheckout" in u),
                    timeout=20_000,
                )
            except PWTimeout:
                # Fallback: maybe SAP took us elsewhere. Continue anyway and
                # the form-fill step will show useful errors.
                logger.warning("[worldline] didn't detect worldline URL after 20s; current URL: {}", page.url)

            page.wait_for_load_state("networkidle", timeout=10_000)

            # Worldline shows a brand-selection page first when multiple
            # methods are configured. Pick the brand matching our card number,
            # then click the "Continue/Proceed" button to go to the form.
            if "PaymentMethods/Selection" in page.url:
                brand = _detect_card_brand(card.number)
                if not brand:
                    _save_diag(page, diagnostics_dir, "unknown-card-brand")
                    return WorldlineResult(success=False, error=f"could not infer brand from card BIN {card.number[:6]}", final_url=page.url)
                logger.info("[worldline] selection page — picking brand={}", brand)
                try:
                    page.locator(f'input[type="radio"][value="{brand}"]').first.click(timeout=5_000)
                except PWError as e:
                    _save_diag(page, diagnostics_dir, f"brand-radio-{brand}")
                    return WorldlineResult(success=False, error=f"could not select {brand}: {e}", final_url=page.url)
                # Click "proceed to payment" — it un-disables once a radio is picked.
                proceed_sel = [
                    'button.proceed-to-payment-button-id:not(.disabled)',
                    'button.proceed-to-payment-button-id',
                    'button[class*="proceed"]:not(.disabled)',
                    'button[class*="proceed"]',
                ]
                proceeded = False
                for sel in proceed_sel:
                    try:
                        el = page.locator(sel).first
                        if el.is_visible(timeout=2_000):
                            el.click()
                            proceeded = True
                            break
                    except PWError:
                        continue
                if not proceeded:
                    # Last resort: submit the selection form via JS
                    try:
                        page.evaluate("document.getElementById('payment-paymentmethodselection').submit()")
                        proceeded = True
                    except PWError as e:
                        _save_diag(page, diagnostics_dir, "no-proceed-button")
                        return WorldlineResult(success=False, error=f"could not proceed past brand selection: {e}", final_url=page.url)
                # Wait for the actual card form to load
                try:
                    page.wait_for_url(lambda u: "Payment/Form" in u or "Selection" not in u, timeout=15_000)
                except PWTimeout:
                    logger.warning("[worldline] still on selection page after click; URL: {}", page.url)
                page.wait_for_load_state("networkidle", timeout=10_000)

            # Fill the card form
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
                if not (m_ok and y_ok):
                    # Try short-year fallback for the year field
                    _try_fill(page, EXPIRY_YEAR_SELECTORS, yy, kind="expiryYearShort")

            cvv_ok = _try_fill(page, CVC_SELECTORS, card.cvv, kind="cvv")
            if not cvv_ok:
                _save_diag(page, diagnostics_dir, "no-cvv")
                return WorldlineResult(success=False, error="CVV input not found", final_url=page.url)

            # Submit
            if not _try_click(page, SUBMIT_SELECTORS):
                _save_diag(page, diagnostics_dir, "no-submit")
                return WorldlineResult(success=False, error="submit button not found", final_url=page.url)

            # Wait for either: success callback URL, OR 3DS iframe to appear
            elapsed = 0
            poll = 0.5
            while elapsed < timeout_s:
                try:
                    cur = page.url
                except Exception:
                    cur = ""
                if "callback" in cur or "kuleuven.cloud" in cur:
                    final_url = cur
                    logger.info("[worldline] reached callback URL: {}", cur)
                    break
                if not needed_3ds and _has_3ds_iframe(page):
                    needed_3ds = True
                    logger.info("[worldline] 3DS challenge detected — DMing user")
                    if on_3ds_challenge:
                        try:
                            on_3ds_challenge()
                        except Exception as e:
                            logger.warning("[worldline] on_3ds_challenge raised: {}", e)
                try:
                    page.wait_for_timeout(int(poll * 1000))
                except PWError:
                    break
                elapsed += poll

            if final_url is None:
                _save_diag(page, diagnostics_dir, "timeout-or-stuck")
                return WorldlineResult(
                    success=False,
                    error=f"timed out after {timeout_s}s waiting for callback",
                    needed_3ds=needed_3ds,
                    final_url=page.url,
                )

            success = "status=success" in final_url or ("kuleuven.cloud" in final_url and "failure" not in final_url)
            return WorldlineResult(success=success, needed_3ds=needed_3ds, final_url=final_url)

        except Exception as e:
            try:
                _save_diag(page, diagnostics_dir, "exception")
            except Exception:
                pass
            return WorldlineResult(success=False, error=f"{type(e).__name__}: {e}", final_url=page.url if page else None)
        finally:
            try:
                browser.close()
            except Exception:
                pass


def _save_diag(page, dirpath: Path, label: str) -> None:
    """On failure, save a screenshot + HTML so we can iterate on selectors."""
    from datetime import datetime
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    base = dirpath / f"{ts}_{label}"
    try:
        page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
    except Exception:
        pass
    try:
        html = page.content()
        base.with_suffix(".html").write_text(html, encoding="utf-8")
    except Exception:
        pass
    try:
        url = page.url
        base.with_suffix(".url.txt").write_text(url, encoding="utf-8")
    except Exception:
        pass
    logger.warning("[worldline] saved diagnostics to {}.*", base)


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
