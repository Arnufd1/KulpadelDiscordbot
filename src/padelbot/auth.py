"""OIDC client for KU Leuven IdP.

Two flows:
  - login(): interactive PKCE auth code flow. User opens URL, signs in + MFA,
    pastes the resulting redirect URL back. We exchange code for tokens.
    Run once on a workstation. Resulting refresh_token gets shipped to the Pi.

  - refresh(): silent — exchange refresh_token for new access_token. Bot uses
    this every time it needs a token. No human, no MFA.
"""
from __future__ import annotations
import base64
import hashlib
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import httpx
from loguru import logger

from .config import settings
from .store import TokenBundle, TokenStore


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) per RFC 7636."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def build_authorize_url() -> tuple[str, str, str]:
    """Returns (authorize_url, state, code_verifier).
    Caller stores state + verifier, points the user at the URL."""
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    params = {
        "response_type": "code",
        "client_id": settings.kul_client_id,
        "redirect_uri": settings.kul_redirect_uri,
        "scope": settings.kul_scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{settings.kul_authorize_url}?{urlencode(params)}", state, verifier


def parse_redirect(callback_url: str, expected_state: str) -> str:
    """Pull `code` out of the redirect URL, verifying state."""
    parsed = urlparse(callback_url)
    qs = parse_qs(parsed.query)
    if "error" in qs:
        raise RuntimeError(f"OIDC error: {qs.get('error')} {qs.get('error_description')}")
    if qs.get("state", [None])[0] != expected_state:
        raise RuntimeError("OIDC state mismatch — possible CSRF, abort.")
    code = qs.get("code", [None])[0]
    if not code:
        raise RuntimeError("No `code` in redirect URL.")
    return code


def _bundle_from_response(data: dict, fallback_refresh: str | None = None) -> TokenBundle:
    """Build a TokenBundle from /token response.
    `fallback_refresh` is used when the IdP doesn't rotate the refresh_token
    (some IdPs return one, some don't on refresh)."""
    return TokenBundle(
        access_token=data["access_token"],
        id_token=data.get("id_token", ""),
        refresh_token=data.get("refresh_token") or fallback_refresh,
        token_type=data.get("token_type", "Bearer"),
        expires_at=time.time() + int(data.get("expires_in", 600)) - 30,  # 30s safety
        scope=data.get("scope", settings.kul_scope),
    )


def exchange_code(code: str, verifier: str, *, warn_no_refresh: bool = False) -> TokenBundle:
    """Auth-code → tokens. Called by login + silent renewal.

    `warn_no_refresh=True` only on initial interactive login — for silent
    renewals we already know the IdP doesn't issue refresh_tokens.
    """
    body = {
        "grant_type": "authorization_code",
        "client_id": settings.kul_client_id,
        "redirect_uri": settings.kul_redirect_uri,
        "code": code,
        "code_verifier": verifier,
    }
    r = httpx.post(settings.kul_token_url, data=body, timeout=30)
    r.raise_for_status()
    data = r.json()
    if warn_no_refresh and "refresh_token" not in data:
        logger.warning(
            "No refresh_token in /token response (expected for KU Leuven). "
            "Bot will rely on session-cookie silent renewal instead."
        )
    return _bundle_from_response(data)


def refresh(bundle: TokenBundle) -> TokenBundle:
    """refresh_token → new access_token. Called by the bot on every wake-up."""
    if not bundle.refresh_token:
        raise RuntimeError("No refresh_token stored — re-login required.")
    body = {
        "grant_type": "refresh_token",
        "client_id": settings.kul_client_id,
        "refresh_token": bundle.refresh_token,
    }
    r = httpx.post(settings.kul_token_url, data=body, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Refresh failed [{r.status_code}]: {r.text}")
    data = r.json()
    return _bundle_from_response(data, fallback_refresh=bundle.refresh_token)


def login_via_browser(
    timeout_seconds: int = 300,
    storage_state_path: "Path | None" = None,
) -> TokenBundle:
    """Launch a real browser, complete the OIDC flow interactively, capture the code.

    Capture strategy: listen to BOTH `request` and `framenavigated` events. The
    `request` event fires for every network request including redirect hops, so
    the auth-callback URL with `?code=` is observable even though the SPA later
    strips it via history.replaceState. We don't abort or interfere — we let the
    SPA load normally so the user sees a clean success state.

    Run this on a workstation. Don't ship Playwright + Chromium to the Pi.
    """
    try:
        from playwright.sync_api import sync_playwright, Error as PWError
    except ImportError as e:
        raise RuntimeError(
            "playwright not installed. Run:\n"
            "  uv pip install \"playwright>=1.45\"\n"
            "  playwright install chromium"
        ) from e

    auth_url, state, verifier = build_authorize_url()
    captured: dict[str, str | None] = {"url": None}

    def matches(url: str) -> bool:
        return "oidc/auth-callback" in url and ("code=" in url or "error=" in url)

    def on_request(req) -> None:
        if captured["url"] is None and matches(req.url):
            captured["url"] = req.url
            logger.info("Captured callback URL via request event")

    def on_framenavigated(frame) -> None:
        if captured["url"] is None and matches(frame.url):
            captured["url"] = frame.url
            logger.info("Captured callback URL via framenavigated event")

    with sync_playwright() as p:
        logger.info("Launching browser. Sign in + tap MFA on your phone...")
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.on("request", on_request)
        page.on("framenavigated", on_framenavigated)
        try:
            page.goto(auth_url)
        except PWError as e:
            logger.warning("Initial navigation error (non-fatal): {}", e)

        elapsed = 0.0
        try:
            while captured["url"] is None and elapsed < timeout_seconds:
                page.wait_for_timeout(100)
                elapsed += 0.1
        except PWError as e:
            if captured["url"] is None:
                browser.close()
                raise RuntimeError(
                    "Browser closed before login completed. "
                    "Make sure to finish MFA before closing the window."
                ) from e

        # CRITICAL: save cookies + close browser IMMEDIATELY.
        # The auth code is single-use. If we let the SPA load its /token call
        # first, the code is burned and our exchange returns 400.
        # By the time on_request fires, the IdP's Set-Cookie headers have
        # already been processed, so cookies are in the context.
        if storage_state_path is not None:
            try:
                storage_state_path.parent.mkdir(parents=True, exist_ok=True)
                ctx.storage_state(path=str(storage_state_path))
                logger.info("Saved browser storage state to {}", storage_state_path)
            except PWError as e:
                logger.warning("Could not save storage state: {}", e)

        try:
            browser.close()
        except PWError:
            pass

    if not captured["url"]:
        raise RuntimeError(
            f"Login timed out after {timeout_seconds}s — never reached redirect_uri."
        )
    code = parse_redirect(captured["url"], state)
    return exchange_code(code, verifier, warn_no_refresh=True)


def silent_refresh_via_session(
    storage_state_path: Path,
    timeout_seconds: int = 8,
    headless: bool = True,
    id_token_hint: str | None = None,  # kept for API compat; unused
) -> TokenBundle:
    """Use saved IdP cookies to get a fresh access_token without MFA.

    Strategy: do a plain /authorize (NOT prompt=none — Shibboleth refuses that
    for the `usc` client). With a recent-MFA Shibboleth session cookie, the IdP
    auto-redirects to auth-callback with a fresh code in milliseconds — no
    interaction, no MFA, no push.

    If the session is stale, the IdP would route through e2s1/e2s2 (MFA stages)
    and trigger a push to the user's phone. We DO NOT want that. So:
      - Tight timeout (default 8s) — a fresh redirect takes ~500ms.
      - Watch for any navigation to e2s1/e2s2 and abort immediately.
    """
    if not storage_state_path.exists():
        raise RuntimeError(
            f"No storage state at {storage_state_path}. Run `padelbot login` first."
        )

    try:
        from playwright.sync_api import sync_playwright, Error as PWError
    except ImportError as e:
        raise RuntimeError("playwright not installed.") from e

    auth_url, state, verifier = build_authorize_url()

    captured: dict[str, str | bool | None] = {
        "url": None,             # successful auth-callback?code=
        "error": None,           # auth-callback?error=
        "mfa_websocket": False,  # browser opened authenticator-wss → MFA is happening
    }

    def on_request(req) -> None:
        url = req.url
        if "oidc/auth-callback" in url:
            if "code=" in url and captured["url"] is None:
                captured["url"] = url
            elif "error=" in url:
                captured["error"] = url

    def on_websocket(ws) -> None:
        # ANY connection to authenticator-wss means the MFA flow is running.
        # That happens regardless of whether the IdP eventually pushes — but if
        # we close the browser before the WS sends/receives LOGIN, the MFA
        # request goes to nothing and the user gets at most a stale push notif.
        if "authenticator-wss" in (ws.url or ""):
            logger.info("MFA WebSocket detected ({}) — bailing.", ws.url)
            captured["mfa_websocket"] = True

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(storage_state=str(storage_state_path))
        page = ctx.new_page()
        page.on("request", on_request)
        page.on("websocket", on_websocket)
        try:
            page.goto(auth_url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
        except PWError:
            pass

        elapsed = 0.0
        while (captured["url"] is None
               and captured["error"] is None
               and not captured["mfa_websocket"]
               and elapsed < timeout_seconds):
            try:
                page.wait_for_timeout(100)
            except PWError:
                break
            elapsed += 0.1

        # If MFA WebSocket opened, abort hard so we don't process a tap.
        if captured["mfa_websocket"] and captured["url"] is None:
            try:
                page.goto("about:blank", timeout=1500)
            except PWError:
                pass

        # Persist any cookie rotations the IdP issued during renewal
        try:
            ctx.storage_state(path=str(storage_state_path))
        except PWError:
            pass
        try:
            browser.close()
        except PWError:
            pass

    if captured["url"]:
        code = parse_redirect(captured["url"], state)
        return exchange_code(code, verifier)

    if captured["mfa_websocket"]:
        raise RuntimeError(
            "Session expired — IdP triggered the MFA WebSocket. Aborted. "
            "Run `padelbot login` to re-authenticate."
        )

    if captured["error"]:
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(captured["error"]).query)
        err = qs.get("error", ["unknown"])[0]
        desc = qs.get("error_description", [""])[0]
        raise RuntimeError(f"Silent refresh refused by IdP: {err} — {desc}")

    raise RuntimeError(
        f"Silent refresh timed out after {timeout_seconds}s — no redirect, no error, no MFA page. "
        "Possibly a network issue or the IdP changed its flow."
    )


def get_access_token(store: TokenStore) -> str:
    """High-level: returns a currently-valid access_token, refreshing if needed.

    KU Leuven doesn't issue refresh_tokens for the `usc` client, so we use the
    Shibboleth IdP session cookie (saved at login) for silent renewal instead.
    """
    bundle = store.load()
    if bundle is None:
        raise RuntimeError("No tokens stored — run `padelbot login` first.")
    if bundle.expires_at > time.time():
        return bundle.access_token
    logger.info("Access token expired; silent-refresh via session cookie...")
    new_bundle = silent_refresh_via_session(settings.padelbot_storage_file)
    store.save(new_bundle)
    return new_bundle.access_token
