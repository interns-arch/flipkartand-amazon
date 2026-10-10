"""Dealer portal authentication.

Logs in with PORTAL_USERNAME / PORTAL_PASSWORD, sends the access token as a Bearer token,
refreshes it with the refresh token when it expires, and falls back to a fresh login
if the refresh token is rejected too.
"""

import asyncio
import base64
import json
import logging
import time

import httpx

from app.config import Settings

log = logging.getLogger(__name__)

ACCESS_KEYS = ("access_token", "accessToken", "access", "token")
REFRESH_KEYS = ("refresh_token", "refreshToken", "refresh")
EXPIRY_KEYS = ("expires_in", "expiresIn")
EXPIRY_MARGIN_SECONDS = 60


class PortalAuthError(Exception):
    pass


def _find(data: dict, keys: tuple[str, ...]):
    """Look for the first matching key at the top level or inside a "data" envelope."""
    for container in (data, data.get("data") if isinstance(data.get("data"), dict) else {}):
        for key in keys:
            if container.get(key):
                return container[key]
    return None


def _jwt_seconds_left(token: str) -> int | None:
    """Read the exp claim of a JWT (not verified; only used to schedule the refresh)."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        left = int(claims["exp"] - time.time())
        return left if left > 0 else None
    except (IndexError, KeyError, TypeError, ValueError):
        return None


class PortalAuth:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        if not (settings.portal_base_url and settings.portal_username and settings.portal_password):
            raise RuntimeError("Dealer portal needs PORTAL_BASE_URL, PORTAL_USERNAME and PORTAL_PASSWORD")
        self.settings = settings
        self.base_url = settings.portal_base_url.rstrip("/")
        self.http = http
        self._access: str | None = None
        self._refresh: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    def _store(self, data: dict) -> None:
        access = _find(data, ACCESS_KEYS)
        if not access:
            raise PortalAuthError("portal auth response has no access token")
        self._access = access
        self._refresh = _find(data, REFRESH_KEYS) or self._refresh
        ttl = _find(data, EXPIRY_KEYS) or _jwt_seconds_left(access) or self.settings.portal_token_ttl_seconds
        self._expires_at = time.time() + int(ttl)

    async def _login(self) -> None:
        resp = await self.http.post(
            f"{self.base_url}{self.settings.portal_login_path}",
            json={
                self.settings.portal_username_field: self.settings.portal_username,
                "password": self.settings.portal_password,
            },
        )
        if resp.is_error:
            raise PortalAuthError(f"portal login failed: HTTP {resp.status_code} {resp.text[:300]}")
        self._store(resp.json())
        log.info("Logged in to dealer portal")

    async def _refresh_or_login(self) -> None:
        if self._refresh:
            resp = await self.http.post(
                f"{self.base_url}{self.settings.portal_refresh_path}",
                json={self.settings.portal_refresh_field: self._refresh},
            )
            if not resp.is_error:
                self._store(resp.json())
                return
            log.info("Portal refresh token rejected (HTTP %s); logging in again", resp.status_code)
        await self._login()

    async def _token(self, stale: str | None = None) -> str:
        async with self._lock:
            # Another request may already have renewed the token while we waited for the lock
            if self._access and self._access != stale and time.time() < self._expires_at - EXPIRY_MARGIN_SECONDS:
                return self._access
            await (self._refresh_or_login() if self._access else self._login())
            return self._access

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Authenticated request to the portal; on 401 renews the token and retries once."""
        token = await self._token()
        resp = await self._send(method, path, token, **kwargs)
        if resp.status_code == 401:
            token = await self._token(stale=token)
            resp = await self._send(method, path, token, **kwargs)
        return resp

    async def _send(self, method: str, path: str, token: str, **kwargs) -> httpx.Response:
        headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {token}"}
        return await self.http.request(method, f"{self.base_url}{path}", headers=headers, **kwargs)
