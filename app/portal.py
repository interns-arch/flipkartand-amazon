"""Adapters for the dealer portal, which is the source of truth for stock.

PORTAL_MODE=local  -> LocalPortal: built-in test store in the local database.
PORTAL_MODE=rest   -> VagminePortal: real dealer portal via the order flow
                      (resolve customer -> analyze -> push order, see app/order_flow.py).
PORTAL_MODE=api    -> OrderApiPortal: each new order is POSTed to ORDER_API_URL; stock comes
                      from what was sent to POST /api/v1/stock.
The portal is expected to reduce its own stock when it creates a seller-fulfilled order.
"""

import logging
from typing import Protocol

import httpx

from app.config import Settings
from app.db import Database
from app.models import ChannelOrder
from app.order_flow import OrderFlow, process_request_from_channel_order
from app.vagmine import FlowError

log = logging.getLogger(__name__)


class DealerPortal(Protocol):
    async def create_order(self, order: ChannelOrder) -> str: ...

    async def get_stock(self, skus: list[str]) -> dict[str, int]: ...


class LocalPortal:
    """Stores orders and stock in the local database, for testing before the real portal is wired in."""

    requires_sku_mapping = True

    def __init__(self, db: Database):
        self.db = db

    async def create_order(self, order: ChannelOrder) -> str:
        return self.db.local_create_order(order)

    async def get_stock(self, skus: list[str]) -> dict[str, int]:
        return self.db.local_get_stock(skus)


class VagminePortal:
    requires_sku_mapping = True

    def __init__(self, flow: OrderFlow):
        self.flow = flow

    async def create_order(self, order: ChannelOrder) -> str:
        request = process_request_from_channel_order(order)
        try:
            record = await self.flow.process(request)
        except FlowError as exc:
            if exc.code == "duplicate_order":  # already pushed earlier: treat as success
                return (exc.details or {}).get("portal_order_id") or request.order_reference_no
            raise
        return record["portal_order_id"] or request.order_reference_no

    async def get_stock(self, skus: list[str]) -> dict[str, int]:
        # No stock-read API from the portal yet. Returning nothing means no quantity is ever
        # pushed to the marketplaces (the sync never sends a stock figure it could not read).
        log.warning("Dealer portal has no stock API configured; skipping stock for %d SKUs", len(skus))
        return {}


class OrderApiPortal:
    """Forwards each marketplace order to ORDER_API_URL.

    Request:  POST ORDER_API_URL, body = ChannelOrder JSON, headers Idempotency-Key: <channel>-<order id>
              (+ Authorization: Bearer ORDER_API_TOKEN when set).
    Any 2xx counts as delivered; 409 counts as "already received". Anything else is retried on the next poll.
    Lines without a SKU mapping are still sent (sku = null, channel_sku = marketplace SKU).
    """

    requires_sku_mapping = False

    def __init__(self, settings: Settings, db: Database, http: httpx.AsyncClient):
        self.settings = settings
        self.db = db
        self.http = http

    async def create_order(self, order: ChannelOrder) -> str:
        if not self.settings.order_api_url:
            raise RuntimeError("ORDER_API_URL is not set yet; the order will be sent once it is")
        reference = f"{order.channel}-{order.channel_order_id}"
        headers = {"Idempotency-Key": reference}
        if self.settings.order_api_token:
            headers["Authorization"] = f"Bearer {self.settings.order_api_token}"
        resp = await self.http.post(self.settings.order_api_url, json=order.model_dump(mode="json"), headers=headers)
        if not (resp.is_success or resp.status_code == 409):
            raise RuntimeError(f"order API returned HTTP {resp.status_code}: {resp.text[:300]}")
        if order.seller_fulfilled:
            sold: dict[str, int] = {}
            for line in order.lines:
                if line.sku:
                    sold[line.sku] = sold.get(line.sku, 0) + line.quantity
            self.db.reduce_synced_stock(sold)
        try:
            body = resp.json()
        except ValueError:
            body = None
        received_id = body.get("id") if isinstance(body, dict) else None
        return str(received_id) if received_id is not None else reference

    async def get_stock(self, skus: list[str]) -> dict[str, int]:
        return self.db.get_synced_stock(skus)


def build_portal(
    settings: Settings, db: Database, flow: OrderFlow | None, http: httpx.AsyncClient
) -> DealerPortal:
    if settings.portal_mode == "api":
        return OrderApiPortal(settings, db, http)
    if settings.portal_mode == "rest":
        if flow is None:
            raise RuntimeError("PORTAL_MODE=rest needs PORTAL_BASE_URL, PORTAL_USERNAME and PORTAL_PASSWORD")
        return VagminePortal(flow)
    return LocalPortal(db)
