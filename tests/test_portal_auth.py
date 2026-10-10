import asyncio
import json

import httpx

from app.config import Settings
from app.portal_auth import PortalAuth

SETTINGS = Settings(
    _env_file=None, portal_base_url="https://portal.test", portal_username="erp-user", portal_password="pw"
)


def run(handler, action):
    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await action(PortalAuth(SETTINGS, http))

    return asyncio.run(main())


def test_logs_in_once_and_sends_bearer_token():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/v1/auth/login":
            assert json.loads(request.content) == {"username": "erp-user", "password": "pw"}
            return httpx.Response(200, json={"access_token": "A1", "refresh_token": "R1", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer A1"
        return httpx.Response(200, json={"ok": True})

    async def action(auth):
        await auth.request("GET", "/api/v1/things")
        return await auth.request("GET", "/api/v1/things")

    assert run(handler, action).status_code == 200
    assert calls.count("/api/v1/auth/login") == 1


def test_expired_token_is_refreshed_and_request_retried():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        path = request.url.path
        if path == "/api/v1/auth/login":
            return httpx.Response(200, json={"data": {"accessToken": "A1", "refreshToken": "R1"}})
        if path == "/api/v1/auth/refresh":
            assert json.loads(request.content) == {"refresh_token": "R1"}
            return httpx.Response(200, json={"access_token": "A2"})
        if request.headers["Authorization"] == "Bearer A1":
            return httpx.Response(401)
        return httpx.Response(200, json={"ok": True})

    resp = run(handler, lambda auth: auth.request("GET", "/api/v1/things"))

    assert resp.status_code == 200
    assert calls == ["/api/v1/auth/login", "/api/v1/things", "/api/v1/auth/refresh", "/api/v1/things"]


def test_rejected_refresh_token_falls_back_to_login():
    logins = []

    def handler(request):
        path = request.url.path
        if path == "/api/v1/auth/login":
            logins.append(1)
            token = f"A{len(logins)}"
            return httpx.Response(200, json={"access_token": token, "refresh_token": "R"})
        if path == "/api/v1/auth/refresh":
            return httpx.Response(401)
        if request.headers["Authorization"] == "Bearer A1":
            return httpx.Response(401)
        return httpx.Response(200)

    resp = run(handler, lambda auth: auth.request("GET", "/api/v1/things"))

    assert resp.status_code == 200
    assert len(logins) == 2
