"""USC OIDC login with PKCE and browser-free session renewal."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode, urlparse, parse_qs

import httpx
from loguru import logger

from .config import settings
from .idp import SessionExpiredError, load_cookies, same_endpoint, save_state, snapshot, walk_oidc
from .store import TokenBundle, TokenStore
from .telemetry import auth_context, current_context, measure


_renewal_lock = threading.RLock()


class NativeAuthRequired(SessionExpiredError):
    """The read dispatcher can park this task while the native lane logs in."""


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_authorize_url() -> tuple[str, str, str]:
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    params = {
        "response_type": "code", "client_id": settings.kul_client_id,
        "redirect_uri": settings.kul_redirect_uri, "scope": settings.kul_scope,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    }
    return f"{settings.kul_authorize_url}?{urlencode(params)}", state, verifier


def parse_redirect(callback_url: str, expected_state: str) -> str:
    if not same_endpoint(callback_url, settings.kul_redirect_uri):
        raise RuntimeError("Unexpected OIDC callback endpoint.")
    qs = parse_qs(urlparse(callback_url).query)
    if qs.get("state") != [expected_state]:
        raise RuntimeError("OIDC state mismatch — aborting login.")
    if "error" in qs:
        raise RuntimeError("The IdP refused the OIDC authorization request.")
    codes = qs.get("code", [])
    if len(codes) != 1 or not codes[0]:
        raise RuntimeError("No unique `code` in redirect URL.")
    return codes[0]


def _bundle_from_response(data: dict, fallback_refresh: str | None = None) -> TokenBundle:
    return TokenBundle(
        access_token=data["access_token"], id_token=data.get("id_token", ""),
        refresh_token=data.get("refresh_token") or fallback_refresh,
        token_type=data.get("token_type", "Bearer"),
        expires_at=time.time() + int(data.get("expires_in", 600)) - 30,
        scope=data.get("scope", settings.kul_scope),
    )


def exchange_code(code: str, verifier: str, *, warn_no_refresh: bool = False) -> TokenBundle:
    response = httpx.post(settings.kul_token_url, data={
        "grant_type": "authorization_code", "client_id": settings.kul_client_id,
        "redirect_uri": settings.kul_redirect_uri, "code": code, "code_verifier": verifier,
    }, timeout=30)
    if response.status_code >= 400:
        raise RuntimeError(f"OIDC code exchange failed [{response.status_code}]. Try a new login.")
    data = response.json()
    if warn_no_refresh and not data.get("refresh_token"):
        logger.info("No refresh token returned; renewal will reuse the saved IdP session.")
    return _bundle_from_response(data)


def refresh(bundle: TokenBundle) -> TokenBundle:
    with measure("refresh_token"):
        return _refresh(bundle)


def _refresh(bundle: TokenBundle) -> TokenBundle:
    if not bundle.refresh_token:
        raise RuntimeError("No refresh token stored; session renewal is required.")
    response = httpx.post(settings.kul_token_url, data={
        "grant_type": "refresh_token", "client_id": settings.kul_client_id,
        "refresh_token": bundle.refresh_token,
    }, timeout=30)
    if response.status_code >= 400:
        # Expired/revoked grants require a new login on the same registered
        # Authenticator. Other protocol, configuration and network failures
        # must not trigger an Android approval or replay a PIN attempt.
        try:
            error = response.json()
        except ValueError:
            error = None
        if (response.status_code in {400, 401} and isinstance(error, dict)
                and error.get("error") == "invalid_grant"):
            raise SessionExpiredError("The saved KUL session expired; a new login is required.")
        raise RuntimeError(f"OIDC refresh failed [{response.status_code}]. Re-login required.")
    return _bundle_from_response(response.json(), fallback_refresh=bundle.refresh_token)


def login_via_browser(timeout_seconds: int = 300, storage_state_path: Path | None = None) -> TokenBundle:
    """Interactive login; block the callback before the SPA can consume its code."""
    try:
        from playwright.sync_api import sync_playwright, Error as PWError
    except ImportError as error:
        raise RuntimeError('Install the login extra and Chromium: pip install -e ".[login]"; playwright install chromium') from error

    auth_url, state, verifier = build_authorize_url()
    captured = []

    def intercept_callback(route):
        captured.append(route.request.url)
        route.abort()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        try:
            context = browser.new_context()
            context.route(lambda url: same_endpoint(url, settings.kul_redirect_uri), intercept_callback)
            page = context.new_page()
            deadline = time.monotonic() + timeout_seconds
            try:
                page.goto(auth_url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
                while not captured and time.monotonic() < deadline:
                    page.wait_for_timeout(100)
            except PWError as error:
                if not captured:
                    raise RuntimeError("Browser login did not complete; finish the MFA step before closing it.") from error
            if not captured:
                raise RuntimeError(f"Login timed out after {timeout_seconds}s.")
            code = parse_redirect(captured[0], state)
            bundle = exchange_code(code, verifier, warn_no_refresh=True)
            if storage_state_path is not None:
                save_state(storage_state_path, context.storage_state())
            return bundle
        finally:
            browser.close()


def _http_login(
    storage_state_path: Path, *, username: str | None = None, password: str | None = None,
    provide_totp: Callable[[], str] | None = None, device_name: str | None = None,
    on_approval: Callable[[str], None] | None = None, timeout_seconds: int = 15,
    authenticate_link: Callable[[str], None] | None = None,
    use_saved_session: bool = True,
) -> TokenBundle:
    existing = json.loads(storage_state_path.read_text(encoding="utf-8")) if use_saved_session and storage_state_path.exists() else {"cookies": [], "origins": []}
    auth_url, state, verifier = build_authorize_url()
    with httpx.Client(timeout=timeout_seconds, follow_redirects=False) as client:
        load_cookies(client, existing)
        callback = walk_oidc(
            client, auth_url, settings.kul_redirect_uri, existing,
            username=username, password=password, provide_totp=provide_totp,
            device_name=device_name, on_approval=on_approval,
            authenticate_link=authenticate_link,
        )
        code = parse_redirect(callback, state)
        bundle = exchange_code(code, verifier)
        save_state(storage_state_path, snapshot(client, existing))
        return bundle


def login_via_http(
    username: str, password: str, storage_state_path: Path, *,
    provide_totp: Callable[[], str] | None = None, device_name: str | None = None,
    on_approval: Callable[[str], None] | None = None,
) -> TokenBundle:
    """Password login without Chromium; second factors remain explicit callbacks."""
    with _renewal_lock:
        return _http_login(storage_state_path, username=username, password=password,
                           provide_totp=provide_totp, device_name=device_name, on_approval=on_approval)


def login_via_android(authenticate_link: Callable[[str], None], *, fresh: bool = False) -> TokenBundle:
    """Passwordless OIDC login on an explicitly selected registered Android."""
    with _renewal_lock, measure("android_login") as measurement:
        def measured_link(link: str) -> None:
            measurement.mark_native()
            with measure("native_mfa"):
                authenticate_link(link)
        # The walker warms the registered device before opening its expiring
        # challenge. Preserve this hook without treating warm-up as MFA success.
        prepare = getattr(authenticate_link, "prepare", None)
        if prepare is not None:
            measured_link.prepare = prepare
        try:
            return _http_login(settings.padelbot_storage_file, authenticate_link=measured_link, use_saved_session=not fresh)
        except BaseException:
            # SIGTERM is translated to SystemExit by the job worker. An
            # interrupted PIN attempt is just as uncertain as an HTTP error;
            # persist its guard before cleanup or another task can reuse it.
            on_failure = getattr(authenticate_link, "on_failure", None)
            if on_failure is not None:
                on_failure()
            raise
        finally:
            close = getattr(authenticate_link, "close", None)
            if close is not None:
                try:
                    close()
                except Exception as error:
                    # Never discard valid tokens or replace the original login
                    # error with a cleanup failure. Keep it visible in the log.
                    logger.error("Android login cleanup failed: {}", type(error).__name__)


def silent_refresh_via_session(
    storage_state_path: Path, timeout_seconds: int = 15, headless: bool = True,
    id_token_hint: str | None = None,
) -> TokenBundle:
    """Reuse cookies/localStorage over HTTP, stopping before credentials or MFA.

    The headless/id_token_hint arguments remain for compatibility with old callers.
    """
    with measure("silent_session"):
        if not storage_state_path.exists():
            raise SessionExpiredError("No saved KUL session; a new login is required.")
        with _renewal_lock:
            return _http_login(storage_state_path, timeout_seconds=timeout_seconds)


def renew_access_token(store: TokenStore) -> str:
    """Force renewal and persist tokens under the shared process lock."""
    from filelock import FileLock
    store.blob_file.parent.mkdir(parents=True, exist_ok=True)
    context = current_context()
    with auth_context(context if context in {"probe", "enrollment"} else "renewal"), _renewal_lock, FileLock(str(store.blob_file) + ".renew.lock", timeout=300):
        bundle = store.load()
        if bundle is None:
            raise RuntimeError("No tokens stored — run `padelbot login` first.")
        try:
            new_bundle = refresh(bundle) if bundle.refresh_token else silent_refresh_via_session(settings.padelbot_storage_file)
        except SessionExpiredError:
            if not settings.padelbot_android_autologin:
                raise
            if os.environ.get("PADELWEB_DEFER_AUTH", "").lower() == "true":
                raise NativeAuthRequired("The saved KUL session requires native authentication.") from None
            from .android import AndroidApprover
            new_bundle = login_via_android(AndroidApprover())
        store.save(new_bundle)
        return new_bundle.access_token


def get_access_token(store: TokenStore) -> str:
    with _renewal_lock:
        bundle = store.load()
        if bundle is None:
            raise RuntimeError("No tokens stored — run `padelbot login` first.")
        if bundle.expires_at > time.time():
            return bundle.access_token
        return renew_access_token(store)
