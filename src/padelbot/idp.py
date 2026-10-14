"""HTTP WebFlow for USC OIDC; protocol reference: breitburg/python-kuleuven.

Dispatch on form fields instead of execution=eNsN numbers. Silent renewal
never submits credentials, sends pushes or executes page JavaScript.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from http.cookiejar import Cookie
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup


class SessionExpiredError(RuntimeError):
    """The IdP requires an interactive login."""


def same_endpoint(left: str, right: str) -> bool:
    a, b = urlsplit(left), urlsplit(right)
    return (a.scheme, a.netloc, a.path) == (b.scheme, b.netloc, b.path)


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".session-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(state, output)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_cookies(client: httpx.Client, state: dict) -> None:
    for item in state.get("cookies", []):
        expires, domain = item.get("expires", -1), item["domain"]
        client.cookies.jar.set_cookie(Cookie(
            version=0, name=item["name"], value=item["value"], port=None, port_specified=False,
            domain=domain, domain_specified=domain.startswith("."), domain_initial_dot=domain.startswith("."),
            path=item.get("path", "/"), path_specified=True, secure=item.get("secure", False),
            expires=int(expires) if expires > 0 else None, discard=expires <= 0,
            comment=None, comment_url=None,
            rest={"HttpOnly": item.get("httpOnly", False), "SameSite": item.get("sameSite", "Lax")},
        ))


def snapshot(client: httpx.Client, state: dict) -> dict:
    cookies = [{
        "name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
        "expires": c.expires if c.expires is not None else -1, "secure": c.secure,
        "httpOnly": c.has_nonstandard_attr("HttpOnly") and c.get_nonstandard_attr("HttpOnly") is not False,
        "sameSite": {"lax": "Lax", "strict": "Strict", "none": "None"}.get(
            str(c.get_nonstandard_attr("SameSite", "Lax")).lower(), "Lax"),
    } for c in client.cookies.jar if not c.is_expired()]
    return {**state, "cookies": cookies}


def form_fields(form) -> dict[str, str]:
    return {i["name"]: i.get("value", "") for i in form.find_all("input")
            if i.get("name") and not i.has_attr("disabled")
            and i.get("type", "text").lower() not in {"submit", "button", "checkbox", "radio"}}


_JS_STRING = r'"(?:\\.|[^"\\])*"'
_STORAGE_WRITE = re.compile(rf'writeLocalStorage\(\s*({_JS_STRING})\s*,\s*({_JS_STRING}|null)\s*\)')


def storage_payload(form, soup, state: dict, origin: str) -> dict[str, str]:
    fields = form_fields(form)
    entry = next((o for o in state.setdefault("origins", []) if o["origin"] == origin), None)
    if entry is None:
        entry = {"origin": origin, "localStorage": []}
        state["origins"].append(entry)
    values = {i["name"]: i["value"] for i in entry["localStorage"]}
    writes = {}
    # Decode literal arguments in Shibboleth's template, never eval JavaScript.
    for script in soup.find_all("script"):
        for match in _STORAGE_WRITE.finditer(script.get_text()):
            key, value = json.loads(match[1]), json.loads(match[2])
            writes[key] = value
            if value:
                values[key] = value
            else:
                values.pop(key, None)
    for name in fields:
        if name.startswith("shib_idp_ls_value."):
            fields[name] = values.get(name.split(".", 1)[1], "")
        elif name.startswith("shib_idp_ls_success."):
            key = name.split(".", 1)[1]
            if f"shib_idp_ls_value.{key}" not in fields and key not in writes:
                raise RuntimeError("Unrecognized IdP storage write; session was not renewed.")
            fields[name] = "true"
        elif name.startswith("shib_idp_ls_exception."):
            fields[name] = ""
        elif name == "shib_idp_ls_supported":
            fields[name] = "true"
    fields["_eventId_proceed"] = ""
    entry["localStorage"] = [{"name": k, "value": v} for k, v in values.items()]
    return fields


def trusted_url(url: str, authorize_url: str) -> str:
    if urlsplit(url)[:2] != urlsplit(authorize_url)[:2]:
        raise RuntimeError("IdP flow attempted to leave the configured identity provider.")
    return url


def approve_push(client, response, soup, authorize_url, device_name, on_approval, timeout):
    import websocket

    buttons = soup.select("button[data-account-id]")
    if device_name:
        buttons = [b for b in buttons if b.get("data-device-name") == device_name]
    if len(buttons) != 1:
        names = ", ".join(b.get("data-device-name", "unnamed") for b in soup.select("button[data-account-id]"))
        raise RuntimeError(f"Choose an Authenticator device with --device. Available: {names}")
    button = buttons[0]
    onload = soup.body.get("onload", "") if soup.body else ""
    args = re.search(r"nextauthwsinit\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*,\s*(?:true|false)\s*,\s*['\"]([^'\"]+)['\"]\s*\)", onload)
    if not args:
        raise RuntimeError("Unrecognized Authenticator WebSocket parameters.")
    server_id, register_id, host = args.groups()
    if urlsplit(host).scheme != "wss" or urlsplit(host).hostname != "authenticator-wss.icts.kuleuven.be":
        raise RuntimeError("Unexpected Authenticator WebSocket server.")
    action = trusted_url(urljoin(str(response.url), button.find_parent("form").get("action", "")), authorize_url)
    origin = f"{urlsplit(authorize_url).scheme}://{urlsplit(authorize_url).netloc}"
    deadline = time.monotonic() + timeout
    try:
        connection = websocket.create_connection(host, timeout=min(10, timeout), origin=origin)
    except websocket.WebSocketException as error:
        raise RuntimeError("Could not connect to KU Leuven Authenticator.") from error
    try:
        connection.send(f"REGISTER {register_id} {server_id}")
        pushed = client.post(action, data={"nextauthAccountId": button["data-account-id"], "_eventId_PushLoginOnAccount": ""}, follow_redirects=False)
        pushed.raise_for_status()
        payload = pushed.json()
        if payload.get("error") or not payload.get("url"):
            raise RuntimeError("Authenticator push could not be started.")
        continuation = trusted_url(urljoin(str(response.url), payload["url"]), authorize_url)
        on_approval(button.get("data-device-name", "your device"))
        ping_at = time.monotonic() + 50
        while time.monotonic() < deadline:
            connection.settimeout(max(0.01, min(ping_at, deadline) - time.monotonic()))
            try:
                message = connection.recv()
            except websocket.WebSocketTimeoutException:
                message = None
            if message == "" or message == b"":
                raise RuntimeError("Authenticator connection closed before approval.")
            if isinstance(message, bytes):
                message = message.decode("utf-8", "replace")
            if message and message.startswith("LOGIN"):
                return client.post(continuation, data={"_eventId_proceed": ""}, follow_redirects=False)
            if time.monotonic() >= ping_at:
                connection.send("PING")
                ping_at = time.monotonic() + 50
        raise RuntimeError(f"Authenticator approval timed out after {timeout}s.")
    except websocket.WebSocketException as error:
        raise RuntimeError("KU Leuven Authenticator connection failed before approval.") from error
    finally:
        connection.close()


def approve_authenticator_link(client, response, soup, authorize_url, authenticate_link, timeout):
    """Bind a local phone action to this QR challenge, without sending a push."""
    import websocket
    from .android import validate_challenge_link

    links = {a.get("href") for a in soup.select("a[href]")
             if a.get("href", "").startswith("https://icts.kuleuven.be/apps/authenticator/")}
    if len(links) != 1:
        raise RuntimeError("No unique Authenticator link on the IdP challenge page.")
    link = validate_challenge_link(links.pop())
    onload = soup.body.get("onload", "") if soup.body else ""
    args = re.search(r"nextauthwsinit\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*,\s*(?:true|false)\s*,\s*['\"]([^'\"]+)['\"]\s*\)", onload)
    form = soup.find("form", id="loginformnextauth")
    if not args or form is None:
        raise RuntimeError("Unrecognized Authenticator challenge page.")
    server_id, register_id, host = args.groups()
    if urlsplit(host).scheme != "wss" or urlsplit(host).hostname != "authenticator-wss.icts.kuleuven.be":
        raise RuntimeError("Unexpected Authenticator WebSocket server.")
    action = trusted_url(urljoin(str(response.url), form.get("action", "")), authorize_url)
    origin = f"{urlsplit(authorize_url).scheme}://{urlsplit(authorize_url).netloc}"
    deadline = time.monotonic() + timeout
    try:
        connection = websocket.create_connection(host, timeout=min(10, timeout), origin=origin)
    except websocket.WebSocketException as error:
        raise RuntimeError("Could not connect to KU Leuven Authenticator.") from error
    try:
        connection.send(f"REGISTER {register_id} {server_id}")
        # Only this link is handed to the Android; never poll unrelated pushes.
        authenticate_link(link)
        ping_at = time.monotonic() + 50
        while time.monotonic() < deadline:
            connection.settimeout(max(0.01, min(ping_at, deadline) - time.monotonic()))
            try:
                message = connection.recv()
            except websocket.WebSocketTimeoutException:
                message = None
            if message == "" or message == b"":
                raise RuntimeError("Authenticator connection closed before approval.")
            if isinstance(message, bytes):
                message = message.decode("utf-8", "replace")
            if message and message.startswith("LOGIN"):
                payload = {**form_fields(form), "_eventId_proceed": ""}
                return client.post(action, data=payload, follow_redirects=False)
            if time.monotonic() >= ping_at:
                connection.send("PING")
                ping_at = time.monotonic() + 50
        raise RuntimeError(f"Android authentication timed out after {timeout}s.")
    except websocket.WebSocketException as error:
        raise RuntimeError("Authenticator connection failed before approval.") from error
    finally:
        connection.close()


def walk_oidc(
    client: httpx.Client, authorize_url: str, redirect_uri: str, state: dict, *,
    username: str | None = None, password: str | None = None,
    provide_totp: Callable[[], str] | None = None, device_name: str | None = None,
    on_approval: Callable[[str], None] | None = None, approval_timeout: int = 120,
    authenticate_link: Callable[[str], None] | None = None,
) -> str:
    """Return the callback without fetching it or consuming its one-use code."""
    request = client.build_request("GET", authorize_url)
    submitted_password = submitted_totp = submitted_push = False
    selected_passwordless = submitted_link = False
    pending_response = None
    for _ in range(24):
        if pending_response is None:
            if same_endpoint(str(request.url), redirect_uri):
                return str(request.url)
            trusted_url(str(request.url), authorize_url)
            response = client.send(request, follow_redirects=False)
        else:
            response, pending_response = pending_response, None
        if response.is_redirect:
            target = urljoin(str(response.url), response.headers["location"])
            if same_endpoint(target, redirect_uri):
                return target
            trusted_url(target, authorize_url)
            method = response.request.method if response.status_code in {307, 308} else "GET"
            request = client.build_request(method, target, content=response.request.content if method == "POST" else None,
                                           headers={"Content-Type": "application/x-www-form-urlencoded"} if method == "POST" else None)
            continue
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        forms = soup.find_all("form")
        if authenticate_link is not None:
            selector = soup.find("form", id="nextauthLoginForm")
            if selector is not None:
                if selected_passwordless:
                    raise RuntimeError("Passwordless authentication returned to the login selector.")
                action = trusted_url(urljoin(str(response.url), selector.get("action", "")), authorize_url)
                payload = {**form_fields(selector), "_eventId_UseNextauth": "", "__Host-rememberDevice": "true"}
                request = client.build_request("POST", action, data=payload)
                selected_passwordless = True
                continue
            if soup.find("form", id="loginformnextauth") is not None:
                if submitted_link:
                    raise RuntimeError("Android login did not finish; no second approval was attempted.")
                pending_response = approve_authenticator_link(client, response, soup, authorize_url, authenticate_link, approval_timeout)
                submitted_link = True
                continue
        form = next((f for f in forms if any(n.startswith("shib_idp_ls_") for n in form_fields(f))), None)
        if form is not None:
            origin = f"{response.url.scheme}://{response.url.netloc.decode()}"
            payload = storage_payload(form, soup, state, origin)
        else:
            form = next((f for f in forms if {"username", "password"} <= form_fields(f).keys()), None)
            if form is not None:
                if username is None or password is None:
                    raise SessionExpiredError("Session expired — run `padelbot login` or `padelbot login --http`.")
                if submitted_password:
                    raise RuntimeError("IdP rejected the credentials; no retry was sent.")
                payload = {**form_fields(form), "username": username, "password": password,
                           "_eventId": "proceed", "rememberlogin": "1"}
                submitted_password = True
            else:
                form = next((f for f in forms if "tokencode" in form_fields(f)), None)
                if form is not None:
                    if provide_totp is None:
                        raise SessionExpiredError("IdP requires a TOTP code; run `padelbot login --http`.")
                    if submitted_totp:
                        raise RuntimeError("IdP rejected the TOTP code; no retry was sent.")
                    payload = {**form_fields(form), "tokencode": provide_totp(), "_eventId": "proceed"}
                    submitted_totp = True
                elif soup.select("button[data-account-id]"):
                    if on_approval is None:
                        raise SessionExpiredError("IdP requires Authenticator approval; run `padelbot login`.")
                    if submitted_push:
                        raise RuntimeError("Authenticator approval failed; no second push was sent.")
                    pending_response = approve_push(client, response, soup, authorize_url, device_name, on_approval, approval_timeout)
                    submitted_push = True
                    continue
                else:
                    raise SessionExpiredError("Unrecognized IdP page; use browser login to inspect the flow.")
        action = trusted_url(urljoin(str(response.url), form.get("action", "")), authorize_url)
        request = client.build_request("POST", action, data=payload)
    raise RuntimeError("IdP flow exceeded the redirect/form limit.")
