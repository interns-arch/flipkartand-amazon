"""Vagmine dealer-portal client: customer resolve, order analyze, push order, track.

Every portal reply is returned or raised with its HTTP status and body, so callers
(and the tester page) can always see exactly what the portal said.
"""

from urllib.parse import quote

import httpx

from app.config import Settings
from app.portal_auth import PortalAuth, PortalAuthError


class FlowError(Exception):
    """An error with the HTTP status our API should return and, when relevant, the portal's reply."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        step: str | None = None,
        portal_status: int | None = None,
        portal_response=None,
        details: dict | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.step = step
        self.portal_status = portal_status
        self.portal_response = portal_response
        self.details = details

    def to_dict(self) -> dict:
        error = {"code": self.code, "message": self.message}
        if self.step:
            error["step"] = self.step
        if self.details:
            error["details"] = self.details
        if self.portal_status is not None:
            error["portal_status"] = self.portal_status
            error["portal_response"] = self.portal_response
        return {"error": error}


def _body(resp: httpx.Response):
    try:
        return resp.json()
    except ValueError:
        return resp.text[:2000]


class VagmineClient:
    def __init__(self, settings: Settings, auth: PortalAuth):
        self.settings = settings
        self.auth = auth

    async def call(self, step: str, method: str, path: str, payload: dict | None = None) -> tuple[int, object]:
        try:
            resp = await self.auth.request(method, path, json=payload)
        except PortalAuthError as exc:
            raise FlowError(502, "portal_auth_failed", str(exc), step) from exc
        except httpx.TimeoutException as exc:
            raise FlowError(504, "portal_timeout", "dealer portal did not respond in time", step) from exc
        except httpx.HTTPError as exc:
            raise FlowError(502, "portal_unreachable", f"could not reach dealer portal: {exc}", step) from exc

        body = _body(resp)
        if resp.is_success:
            return resp.status_code, body
        if resp.status_code in (401, 403):
            raise FlowError(502, "portal_auth_failed", "dealer portal rejected our credentials or permissions",
                            step, resp.status_code, body)
        if resp.status_code == 409:
            raise FlowError(409, "portal_conflict", "dealer portal reports a conflict (e.g. duplicate order)",
                            step, resp.status_code, body)
        if resp.status_code < 500:
            raise FlowError(422, "portal_rejected", "dealer portal rejected the request", step, resp.status_code, body)
        raise FlowError(502, "portal_error", "dealer portal returned a server error", step, resp.status_code, body)

    async def resolve_customer(self, payload: dict) -> tuple[int, object]:
        return await self.call("resolve", "POST", self.settings.portal_resolve_path, payload)

    async def analyze(self, payload: dict) -> tuple[int, object]:
        return await self.call("analyze", "POST", self.settings.portal_analyze_path, payload)

    async def push_order(self, payload: dict) -> tuple[int, object]:
        return await self.call("push_order", "POST", self.settings.portal_push_path, payload)

    async def track(self, order_reference_no: str) -> tuple[int, object]:
        template = self.settings.portal_track_path
        if not template:
            raise FlowError(501, "tracking_not_configured",
                            "tracking API not configured yet (set PORTAL_TRACK_PATH once Shubham shares the docs)",
                            "track")
        path = template.replace("{order_reference_no}", quote(order_reference_no, safe=""))
        return await self.call("track", "GET", path)
