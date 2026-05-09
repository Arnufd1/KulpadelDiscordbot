"""HTTP client for the Delcom backbone-web-api.

Wraps httpx with the auth + standard headers every request needs. Lazy-fetches
a fresh access_token via auth.get_access_token (which uses session-cookie
silent renewal under the hood).
"""
from __future__ import annotations
import json as _json
from typing import Any
from urllib.parse import urlencode

import httpx
from loguru import logger

from .auth import get_access_token
from .config import settings
from .store import TokenStore


class BackboneClient:
    """Synchronous client. One per booking run is fine — we don't need pooling."""

    def __init__(self, store: TokenStore | None = None, *, x_user_role_id: int | None = None):
        self._store = store or TokenStore(settings.padelbot_key_file, settings.padelbot_token_file)
        self._x_user_role_id = x_user_role_id  # resolved lazily from /auth if None
        self._http = httpx.Client(base_url=settings.backbone_api_base, timeout=15)

    # --- internals ---

    def _headers(self, *, write: bool = False) -> dict[str, str]:
        token = get_access_token(self._store)
        h = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://usc.kuleuven.cloud",
            "Referer": "https://usc.kuleuven.cloud/",
            "x-custom-lang": "nl",
            "x-platform": "CF",
        }
        if self._x_user_role_id is not None:
            h["x-user-role-id"] = str(self._x_user_role_id)
        if write:
            h["Content-Type"] = "application/json"
        return h

    def _maybe_resolve_role_id(self) -> None:
        if self._x_user_role_id is not None:
            return
        me = self.me()
        roles = me.get("userRoles") or []
        if not roles:
            raise RuntimeError("No userRoles on /auth response — cannot derive x-user-role-id")
        # Real Customer role first; fallback to any approved role.
        for r in roles:
            if (r.get("role") or {}).get("description", "").lower() == "customer":
                self._x_user_role_id = r["id"]
                return
        self._x_user_role_id = roles[0]["id"]

    def _raise(self, r: httpx.Response, ctx: str) -> None:
        try:
            body = r.json()
        except Exception:
            body = r.text[:500]
        logger.error("API call failed [{}]: {} {} — {}", ctx, r.status_code, r.reason_phrase, body)
        r.raise_for_status()

    # --- public ---

    def me(self) -> dict[str, Any]:
        """GET /auth — current user. Used to discover member_id and x-user-role-id."""
        r = self._http.get("/auth", params={"cf": 0}, headers=self._headers())
        if r.status_code >= 400:
            self._raise(r, "GET /auth")
        return r.json()

    def get(self, path: str, params: dict | None = None) -> Any:
        self._maybe_resolve_role_id()
        # backbone-web-api uses a custom `s` query for filters: a JSON-encoded object.
        if params and "s" in params and not isinstance(params["s"], str):
            params = {**params, "s": _json.dumps(params["s"], separators=(",", ":"))}
        r = self._http.get(path, params=params, headers=self._headers())
        if r.status_code >= 400:
            self._raise(r, f"GET {path}")
        return r.json()

    def post(self, path: str, body: dict) -> Any:
        self._maybe_resolve_role_id()
        r = self._http.post(path, json=body, headers=self._headers(write=True))
        if r.status_code >= 400:
            self._raise(r, f"POST {path}")
        return r.json()

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "BackboneClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
