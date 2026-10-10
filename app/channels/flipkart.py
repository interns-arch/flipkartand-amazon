"""Flipkart Seller API v3 connector (self-access application)."""

import json
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone

import httpx

from app.config import Settings
from app.models import ChannelOrder, OrderLine, PushResult, SkuMapping

log = logging.getLogger(__name__)

# Pre-dispatch states in which an order still needs fulfilling by us
OPEN_STATES = ["APPROVED", "PACKING_IN_PROGRESS", "FORM_FAILED", "PACKED", "READY_TO_DISPATCH"]
INVENTORY_BATCH_SIZE = 10
SHIPMENT_DETAIL_BATCH_SIZE = 25


def _fk_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _line_total(item: dict) -> float | None:
    prices = item.get("priceComponents") or {}
    value = prices.get("sellingPrice", prices.get("customerPrice"))
    return float(value) if value is not None else None


class FlipkartChannel:
    name = "flipkart"

    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        # FLIPKART_LOCATION_ID is only needed for stock updates (see push_stock)
        if not (settings.flipkart_app_id and settings.flipkart_app_secret):
            raise RuntimeError("Flipkart needs FLIPKART_APP_ID and FLIPKART_APP_SECRET")
        self.base = settings.flipkart_base_url.rstrip("/")
        self.app_id = settings.flipkart_app_id
        self.app_secret = settings.flipkart_app_secret
        self.location_id = settings.flipkart_location_id
        self.http = http
        self._token: str | None = None
        self._token_expires = 0.0

    async def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires - 300:
            return self._token
        resp = await self.http.get(
            f"{self.base}/oauth-service/oauth/token",
            params={"grant_type": "client_credentials", "scope": "Seller_Api"},
            auth=(self.app_id, self.app_secret),
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires = time.time() + int(data.get("expires_in", 3600))
        return self._token

    async def _call(self, method: str, path: str, **kwargs) -> dict:
        token = await self._access_token()
        resp = await self.http.request(
            method, f"{self.base}{path}", headers={"Authorization": f"Bearer {token}"}, **kwargs
        )
        if resp.status_code == 401:
            self._token = None
        resp.raise_for_status()
        return resp.json()

    async def fetch_orders(self, since: datetime, is_known: Callable[[str], bool]) -> list[ChannelOrder]:
        body = {
            "filter": {
                "type": "preDispatch",
                "states": OPEN_STATES,
                "orderDate": {"from": _fk_time(since), "to": _fk_time(datetime.now(timezone.utc))},
            },
            "pagination": {"pageSize": 20},
        }
        shipments: list[dict] = []
        page = await self._call("POST", "/sellers/v3/shipments/filter/", json=body)
        while True:
            shipments.extend(page.get("shipments") or [])
            next_url = page.get("nextPageUrl")
            if not page.get("hasMore") or not next_url:
                break
            if not next_url.startswith("/sellers"):
                next_url = f"/sellers{next_url}"
            page = await self._call("GET", next_url)

        orders: dict[str, ChannelOrder] = {}
        shipment_ids: dict[str, set[str]] = {}
        seen_items: set[str] = set()
        for shipment in shipments:
            for item in shipment.get("orderItems") or []:
                order_id = item["orderId"]
                item_id = str(item.get("orderItemId"))
                if item_id in seen_items or is_known(order_id):
                    continue
                seen_items.add(item_id)
                order = orders.setdefault(
                    order_id,
                    ChannelOrder(
                        channel=self.name,
                        channel_order_id=order_id,
                        status=item.get("status", "APPROVED"),
                        ordered_at=item.get("orderDate"),
                    ),
                )
                order.lines.append(
                    OrderLine(
                        channel_sku=item["sku"],
                        quantity=int(item.get("quantity", 1)),
                        line_total=_line_total(item),
                        title=item.get("title"),
                        channel_line_id=item_id,
                    )
                )
                shipment_ids.setdefault(order_id, set()).add(shipment["shipmentId"])

        for order in orders.values():
            totals = [line.line_total for line in order.lines if line.line_total is not None]
            order.total = sum(totals) if totals else None

        await self._attach_addresses(orders, shipment_ids)
        return list(orders.values())

    async def _attach_addresses(self, orders: dict[str, ChannelOrder], shipment_ids: dict[str, set[str]]) -> None:
        all_ids = sorted({sid for ids in shipment_ids.values() for sid in ids})
        addresses: dict[str, dict] = {}
        try:
            for i in range(0, len(all_ids), SHIPMENT_DETAIL_BATCH_SIZE):
                batch = ",".join(all_ids[i : i + SHIPMENT_DETAIL_BATCH_SIZE])
                data = await self._call("GET", f"/sellers/v3/shipments/{batch}")
                for shipment in data.get("shipments") or []:
                    if shipment.get("deliveryAddress"):
                        addresses[shipment["shipmentId"]] = shipment["deliveryAddress"]
        except httpx.HTTPError:
            log.warning("Could not fetch Flipkart shipment addresses; orders will be created without them", exc_info=True)
            return
        for order_id, ids in shipment_ids.items():
            order_address = next((addresses[s] for s in sorted(ids) if s in addresses), None)
            orders[order_id].shipping_address = order_address

    async def push_stock(self, items: list[tuple[SkuMapping, int]]) -> list[PushResult]:
        if not self.location_id:
            return [PushResult(portal_sku=m.portal_sku, quantity=qty, ok=False, message="FLIPKART_LOCATION_ID not set")
                    for m, qty in items]
        results: list[PushResult] = []
        for i in range(0, len(items), INVENTORY_BATCH_SIZE):
            body: dict[str, dict] = {}
            sent: dict[str, tuple[SkuMapping, int]] = {}
            for mapping, qty in items[i : i + INVENTORY_BATCH_SIZE]:
                if not mapping.flipkart_fsn:
                    results.append(
                        PushResult(portal_sku=mapping.portal_sku, quantity=qty, ok=False, message="missing flipkart_fsn")
                    )
                    continue
                body[mapping.flipkart_sku] = {
                    "product_id": mapping.flipkart_fsn,
                    "locations": [{"id": self.location_id, "inventory": qty}],
                }
                sent[mapping.flipkart_sku] = (mapping, qty)
            if not body:
                continue
            data = await self._call("POST", "/sellers/listings/v3/update/inventory", json=body)
            for fk_sku, (mapping, qty) in sent.items():
                outcome = data.get(fk_sku) or {}
                ok = outcome.get("status") == "SUCCESS"
                results.append(
                    PushResult(
                        portal_sku=mapping.portal_sku,
                        quantity=qty,
                        ok=ok,
                        message="" if ok else json.dumps(outcome)[:500],
                    )
                )
        return results
