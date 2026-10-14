import base64
import hashlib
import json
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs

import httpx
import pytest
from bs4 import BeautifulSoup
from click.testing import CliRunner

from padelbot import auth, idp
from padelbot.cli import cli
from padelbot.config import settings
from padelbot.store import TokenBundle, TokenStore


AUTHORIZE = settings.kul_authorize_url + "?state=test"
CALLBACK = settings.kul_redirect_uri + "?state=test&code=one-use-code"
ORIGIN = "https://idp.kuleuven.be"
PASSWORD = '<form method="post" action="?execution=e4s9"><input name="csrf_token" value="csrf"><input name="username"><input name="password"><input name="_eventId" value="proceed"></form>'
TOTP = '<form action="?execution=e8s2"><input name="tokencode"><input name="csrf_token" value="second-csrf"></form>'
STORAGE = '<form action="?execution=e7s4"><input name="shib_idp_ls_value.shib_idp_session_ss"><input name="shib_idp_ls_success.shib_idp_session_ss" value="false"><input name="shib_idp_ls_supported"><input name="_eventId_proceed"></form>'
WRITE = '<form action="?execution=e9s3"><input name="shib_idp_ls_success.shib_idp_session_ss" value="false"><input name="_eventId_proceed"></form><script>writeLocalStorage("shib_idp_session_ss", "rotated-session");</script>'
PUSH = '''<body onload="nextauthwsinit('server','register',false,'wss&#x3a;&#x2f;&#x2f;authenticator-wss.icts.kuleuven.be');"><form action="?execution=e7s1"><button data-account-id="device-id" data-device-name="My phone"></button><input name="_eventId_proceed"></form></body>'''


def run_flow(handler, state=None, **kwargs):
    # The walker must suppress automatic redirects even if the caller enables them.
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        return idp.walk_oidc(client, AUTHORIZE, settings.kul_redirect_uri, state or {"cookies": [], "origins": []}, **kwargs)


def test_pkce_and_state():
    url, state, verifier = auth.build_authorize_url()
    qs = parse_qs(httpx.URL(url).query.decode())
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert qs["code_challenge"] == [challenge]
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["state"] == [state]
    assert 43 <= len(verifier) <= 128


@pytest.mark.parametrize("callback", [
    settings.kul_redirect_uri + "?state=wrong&code=secret",
    settings.kul_redirect_uri + "?state=test&state=test&code=secret",
    settings.kul_redirect_uri + "?state=test&code=a&code=b",
    settings.kul_redirect_uri + "?state=test",
    settings.kul_redirect_uri + "?state=test&error=denied&error_description=secret",
    "https://attacker.example/oidc/auth-callback?state=test&code=secret",
])
def test_callback_rejects_invalid_or_ambiguous_input(callback):
    with pytest.raises(RuntimeError) as error:
        auth.parse_redirect(callback, "test")
    assert "secret" not in str(error.value)


def test_callback_is_never_requested():
    requests = []
    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": CALLBACK})
    assert run_flow(handler) == CALLBACK
    assert requests == [AUTHORIZE]
    assert auth.parse_redirect(CALLBACK, "test") == "one-use-code"


@pytest.mark.parametrize("html", [PASSWORD, TOTP, PUSH])
def test_silent_renewal_stops_before_credentials_or_mfa(html, monkeypatch):
    monkeypatch.setattr(idp, "approve_push", lambda *args: pytest.fail("silent flow sent a push"))
    requests = []
    def handler(request):
        requests.append(request.method)
        return httpx.Response(200, text=html)
    with pytest.raises(idp.SessionExpiredError):
        run_flow(handler)
    assert requests == ["GET"]


def test_reads_and_rotates_saved_localstorage_with_cookie():
    state = {"origins": [{"origin": ORIGIN, "localStorage": [{"name": "shib_idp_session_ss", "value": "old-session"}]}]}
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, text=STORAGE, headers={"Set-Cookie": "JSESSIONID=session; Path=/idp; Secure; HttpOnly"})
        if len(requests) == 2:
            assert parse_qs(request.content.decode())["shib_idp_ls_value.shib_idp_session_ss"] == ["old-session"]
            assert "JSESSIONID=session" in request.headers["cookie"]
            return httpx.Response(200, text=WRITE)
        return httpx.Response(302, headers={"Location": CALLBACK})
    assert run_flow(handler, state) == CALLBACK
    assert len(requests) == 3
    assert state["origins"][0]["localStorage"] == [{"name": "shib_idp_session_ss", "value": "rotated-session"}]


def test_unknown_storage_write_does_not_claim_success():
    html = '<form><input name="shib_idp_ls_success.unknown"></form>'
    with pytest.raises(RuntimeError, match="Unrecognized IdP storage write"):
        run_flow(lambda r: httpx.Response(200, text=html))


def test_storage_delete_preserves_other_origins():
    html = WRITE.replace('"rotated-session"', 'null')
    state = {"origins": [{"origin": ORIGIN, "localStorage": [{"name": "shib_idp_session_ss", "value": "old"}]}, {"origin": "https://example.com", "localStorage": [{"name": "a", "value": "b"}]}]}
    soup = BeautifulSoup(html, "html.parser")
    payload = idp.storage_payload(soup.form, soup, state, ORIGIN)
    assert payload["shib_idp_ls_success.shib_idp_session_ss"] == "true"
    assert state["origins"][0]["localStorage"] == []
    assert state["origins"][1]["localStorage"] == [{"name": "a", "value": "b"}]


@pytest.mark.parametrize("kind", ["redirect", "form"])
def test_credentials_never_leave_idp(kind):
    requests = []
    def handler(request):
        requests.append(request)
        if kind == "redirect":
            return httpx.Response(302, headers={"Location": "https://attacker.example/login"})
        return httpx.Response(200, text=PASSWORD.replace('?execution=e4s9', 'https://attacker.example/login'))
    with pytest.raises(RuntimeError, match="leave"):
        run_flow(handler, username="fake-user", password="fake-secret")
    assert len(requests) == 1


def test_password_then_totp_preserves_hidden_fields():
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, text=PASSWORD)
        if len(requests) == 2:
            payload = parse_qs(request.content.decode())
            assert payload["csrf_token"] == ["csrf"]
            assert payload["password"] == ["fake-secret"]
            return httpx.Response(200, text=TOTP)
        assert parse_qs(request.content.decode())["tokencode"] == ["123456"]
        assert parse_qs(request.content.decode())["csrf_token"] == ["second-csrf"]
        return httpx.Response(302, headers={"Location": CALLBACK})
    assert run_flow(handler, username="fake-user", password="fake-secret", provide_totp=lambda: "123456") == CALLBACK


@pytest.mark.parametrize("html, kwargs, message", [
    (PASSWORD, {"username": "fake-user", "password": "fake-secret"}, "credentials"),
    (TOTP, {"provide_totp": lambda: "123456"}, "TOTP"),
])
def test_bad_credentials_and_totp_are_not_retried(html, kwargs, message):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=html)
    with pytest.raises(RuntimeError, match=message):
        run_flow(handler, **kwargs)
    assert len(requests) == 2


def test_explicit_push_waits_for_login_and_handles_inline_continuation(monkeypatch):
    import websocket
    events = []
    class FakeSocket:
        def __init__(self):
            self.messages = iter(["LOGOUT", "CONFIRMPROVOKE", "LOGIN approved"])
        def send(self, value): events.append(value)
        def recv(self): return next(self.messages)
        def settimeout(self, value): pass
        def close(self): events.append("close")
    monkeypatch.setattr(websocket, "create_connection", lambda *a, **k: FakeSocket())
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, text=PUSH)
        data = parse_qs(request.content.decode(), keep_blank_values=True)
        if "_eventId_PushLoginOnAccount" in data:
            assert events == ["REGISTER register server"]
            events.append("push")
            return httpx.Response(200, json={"url": "?execution=e7s2", "error": ""})
        if "_eventId_proceed" in data and "shib_idp_ls_success.shib_idp_session_ss" not in data:
            return httpx.Response(200, text=WRITE)
        return httpx.Response(302, headers={"Location": CALLBACK})
    assert run_flow(handler, on_approval=lambda device: events.append(device)) == CALLBACK
    assert events == ["REGISTER register server", "push", "My phone", "close"]


def test_cookie_attributes_and_expiry_survive_roundtrip():
    state = {"cookies": [
        {"name": "session", "value": "saved", "domain": "idp.kuleuven.be", "path": "/idp", "expires": -1, "secure": True, "httpOnly": True, "sameSite": "Strict"},
        {"name": "expired", "value": "old", "domain": "idp.kuleuven.be", "expires": 1},
    ], "origins": []}
    with httpx.Client() as client:
        idp.load_cookies(client, state)
        assert client.build_request("GET", AUTHORIZE).headers["cookie"] == "session=saved"
        assert "cookie" not in client.build_request("GET", "http://idp.kuleuven.be/idp").headers
        assert "cookie" not in client.build_request("GET", "https://attacker.example/idp").headers
        result = idp.snapshot(client, state)
    assert result["cookies"] == [state["cookies"][0]]


def make_store(tmp_path):
    store = TokenStore(tmp_path / "key", tmp_path / "tokens")
    store.init_key()
    store.save(TokenBundle("expired", "", "refresh-token", "Bearer", 0, "openid"))
    return store


def test_concurrent_workers_renew_once_and_use_real_refresh_token(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    calls = []
    barrier = threading.Barrier(6)
    def renew(bundle):
        calls.append(bundle.refresh_token)
        time.sleep(0.03)
        return TokenBundle("fresh", "", "rotated", "Bearer", time.time() + 600, "openid")
    monkeypatch.setattr(auth, "refresh", renew)
    monkeypatch.setattr(auth, "silent_refresh_via_session", lambda *a: pytest.fail("ignored refresh token"))
    def worker():
        barrier.wait()
        return auth.get_access_token(store)
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert list(pool.map(lambda _: worker(), range(6))) == ["fresh"] * 6
    assert calls == ["refresh-token"]
    assert store.load().refresh_token == "rotated"


def test_atomic_token_save_preserves_previous_state_on_failure(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    original = store.blob_file.read_bytes()
    monkeypatch.setattr("padelbot.store.os.replace", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        store.save(TokenBundle("new", "", None, "Bearer", 0, "openid"))
    assert store.blob_file.read_bytes() == original
    assert not list(tmp_path.glob(".tokens-*.tmp"))


def test_http_login_persists_state_only_after_code_exchange(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    idp.save_state(path, {"cookies": [], "origins": []})
    original = path.read_bytes()
    monkeypatch.setattr(auth, "build_authorize_url", lambda: (AUTHORIZE, "test", "verifier"))
    factory = httpx.Client
    def handler(request):
        return httpx.Response(302, headers={"Location": CALLBACK, "Set-Cookie": "session=rotated; Secure; HttpOnly; Path=/idp"})
    monkeypatch.setattr(auth.httpx, "Client", lambda **k: factory(transport=httpx.MockTransport(handler), **k))
    def fail_exchange(*args): raise RuntimeError("code rejected")
    monkeypatch.setattr(auth, "exchange_code", fail_exchange)
    with pytest.raises(RuntimeError, match="code rejected"):
        auth.silent_refresh_via_session(path)
    assert path.read_bytes() == original
    monkeypatch.setattr(auth, "exchange_code", lambda *a: TokenBundle("fresh", "", None, "Bearer", 600, "openid"))
    assert auth.silent_refresh_via_session(path).access_token == "fresh"
    assert json.loads(path.read_text())["cookies"][0]["value"] == "rotated"


def test_browser_blocks_spa_callback_before_exchanging(monkeypatch, tmp_path):
    events = []
    class PWError(Exception): pass
    class Browser:
        def new_context(self): return Context()
        def close(self): events.append("closed")
    class Context:
        def route(self, matcher, handler): self.matcher, self.handler = matcher, handler
        def new_page(self): return Page(self)
        def storage_state(self): return {"cookies": [], "origins": []}
    class Page:
        def __init__(self, context): self.context = context
        def goto(self, *args, **kwargs):
            assert self.context.matcher(CALLBACK)
            assert not self.context.matcher("https://attacker.example/oidc/auth-callback")
            route = types.SimpleNamespace(request=types.SimpleNamespace(url=CALLBACK), abort=lambda: events.append("aborted"))
            self.context.handler(route)
            raise PWError("navigation aborted")
    class Playwright:
        chromium = types.SimpleNamespace(launch=lambda **k: Browser())
        def __enter__(self): return self
        def __exit__(self, *a): pass
    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright, module.Error = Playwright, PWError
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    monkeypatch.setattr(auth, "build_authorize_url", lambda: (AUTHORIZE, "test", "verifier"))
    def exchange(*a, **k):
        assert events == ["aborted"]
        return TokenBundle("fresh", "", None, "Bearer", 600, "openid")
    monkeypatch.setattr(auth, "exchange_code", exchange)
    assert auth.login_via_browser(storage_state_path=tmp_path / "state.json").access_token == "fresh"
    assert events == ["aborted", "closed"]


def test_cli_login_requires_key_before_prompt_or_network(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "padelbot_key_file", tmp_path / "missing")
    result = CliRunner().invoke(cli, ["login", "--http"])
    assert result.exit_code == 1
    assert "init-key" in result.output


def test_cli_status_checks_session_without_refresh_token(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    bundle = store.load()
    bundle.refresh_token = None
    store.save(bundle)
    monkeypatch.setattr("padelbot.cli._store", lambda: store)
    calls = []
    monkeypatch.setattr("padelbot.cli.renew_access_token", lambda s: calls.append(s))
    result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 0
    assert calls == [store]


def test_expired_token_without_refresh_token_uses_http_session(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    bundle = store.load()
    bundle.refresh_token = None
    store.save(bundle)
    monkeypatch.setattr(auth, "silent_refresh_via_session", lambda path: TokenBundle("renewed", "", None, "Bearer", time.time() + 600, "openid"))
    assert auth.get_access_token(store) == "renewed"
    assert store.load().access_token == "renewed"


def test_keepalive_recovers_after_failures_without_restart_or_repeated_dm(monkeypatch):
    import asyncio
    from padelbot.discord_bot import PadelBot
    calls, messages, sleeps = [], [], []
    class Owner:
        async def send(self, message): messages.append(message)
    class FakeBot:
        async def _owner(self): return Owner()
    def renew(store):
        calls.append(store)
        if len(calls) < 3:
            raise idp.SessionExpiredError("re-login required")
        return "fresh"
    async def sleep(delay):
        sleeps.append(delay)
        if len(sleeps) == 4:
            raise asyncio.CancelledError()
    monkeypatch.setattr("padelbot.discord_bot.renew_access_token", renew)
    monkeypatch.setattr("padelbot.discord_bot.asyncio.sleep", sleep)
    bot = FakeBot()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(PadelBot._run_keepalive(bot))
    assert len(calls) == 3
    assert len(messages) == 1
    assert bot._last_keepalive_ok is True
