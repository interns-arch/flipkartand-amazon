"""Amazon Selling Partner API connector (private, self-authorised app).

Only seller-fulfilled (MFN, incl. Easy Ship) orders are pulled: FBA orders ship from
Amazon's own stock and must not reduce dealer-portal stock.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from app.config import Settings
from app.models import ChannelOrder, OrderLine, PushResult, SkuMapping

log = logging.getLogger(__name__)

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
OPEN_STATUSES = "Unshipped,PartiallyShipped"
MAX_RETRIES = 4


def _amount(money: dict | None) -> float | None:
    if not money or money.get("Amount") is None:
        return None
    return float(money["Amount"])


class AmazonChannel:
    name = "amazon"
    # Pauses between calls to stay inside SP-API rate limits (getOrderItems: 0.5 req/s; listings: 5 req/s)
    order_items_delay = 2.0
    listings_delay = 0.25

    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        # AMAZON_SELLER_ID is only needed for stock updates (see push_stock)
        if not (settings.amazon_client_id and settings.amazon_client_secret and settings.amazon_refresh_token):
            raise RuntimeError("Amazon needs AMAZON_CLIENT_ID, AMAZON_CLIENT_SECRET and AMAZON_REFRESH_TOKEN")
        self.settings = settings
        self.endpoint = settings.amazon_endpoint.rstrip("/")
        self.marketplace_id = settings.amazon_marketplace_id
        self.http = http
        self._token: str | None = None
        self._token_expires = 0.0

    async def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires - 300:
            return self._token
        resp = await self.http.post(
            LWA_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.settings.amazon_refresh_token,
                "client_id": self.settings.amazon_client_id,
                "client_secret": self.settings.amazon_client_secret,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires = time.time() + int(data.get("expires_in", 3600))
        return self._token

    async def _call(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None) -> dict:
        for attempt in range(MAX_RETRIES):
            token = await self._access_token()
            resp = await self.http.request(
                method,
                f"{self.endpoint}{path}",
                params=params,
                json=json,
                headers={"x-amz-access-token": token},
            )
            if resp.status_code == 429 and attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2 ** (attempt + 1))
                continue
            if resp.status_code in (401, 403):
                self._token = None
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("unreachable")

    async def fetch_orders(self, since: datetime, is_known: Callable[[str], bool]) -> list[ChannelOrder]:
        params: dict = {
            "MarketplaceIds": self.marketplace_id,
            "LastUpdatedAfter": since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "OrderStatuses": OPEN_STATUSES,
            "FulfillmentChannels": "MFN",
        }
        raw_orders: list[dict] = []
        while True:
            payload = (await self._call("GET", "/orders/v0/orders", params=params)).get("payload") or {}
            raw_orders.extend(payload.get("Orders") or [])
            next_token = payload.get("NextToken")
            if not next_token:
                break
            params = {"MarketplaceIds": self.marketplace_id, "NextToken": next_token}

        orders: list[ChannelOrder] = []
        for raw in raw_orders:
            order_id = raw["AmazonOrderId"]
            if is_known(order_id):
                continue
            items = await self._order_items(order_id)
            orders.append(
                ChannelOrder(
                    channel=self.name,
                    channel_order_id=order_id,
                    status=raw.get("OrderStatus", ""),
                    ordered_at=raw.get("PurchaseDate"),
                    seller_fulfilled=raw.get("FulfillmentChannel", "MFN") == "MFN",
                    currency=(raw.get("OrderTotal") or {}).get("CurrencyCode", "INR"),
                    total=_amount(raw.get("OrderTotal")),
                    # Full buyer name/address needs the restricted Direct-to-Consumer Shipping role + RDT
                    shipping_address=raw.get("ShippingAddress"),
                    lines=[
                        OrderLine(
                            channel_sku=item["SellerSKU"],
                            quantity=int(item.get("QuantityOrdered", 1)),
                            line_total=_amount(item.get("ItemPrice")),
                            title=item.get("Title"),
                            channel_line_id=item.get("OrderItemId"),
                        )
                        for item in items
                    ],
                )
            )
        return orders

    async def _order_items(self, order_id: str) -> list[dict]:
        items: list[dict] = []
        params: dict | None = None
        while True:
            payload = (await self._call("GET", f"/orders/v0/orders/{order_id}/orderItems", params=params)).get(
                "payload"
            ) or {}
            items.extend(payload.get("OrderItems") or [])
            await asyncio.sleep(self.order_items_delay)
            if not payload.get("NextToken"):
                return items
            params = {"NextToken": payload["NextToken"]}

    async def push_stock(self, items: list[tuple[SkuMapping, int]]) -> list[PushResult]:
        if not self.settings.amazon_seller_id:
            return [PushResult(portal_sku=m.portal_sku, quantity=qty, ok=False, message="AMAZON_SELLER_ID not set")
                    for m, qty in items]
        results: list[PushResult] = []
        for mapping, qty in items:
            path = f"/listings/2021-08-01/items/{self.settings.amazon_seller_id}/{quote(mapping.amazon_sku, safe='')}"
            body = {
                "productType": "PRODUCT",
                "patches": [
                    {
                        "op": "replace",
                        "path": "/attributes/fulfillment_availability",
                        "value": [{"fulfillment_channel_code": "DEFAULT", "quantity": qty}],
                    }
                ],
            }
            try:
                data = await self._call("PATCH", path, params={"marketplaceIds": self.marketplace_id}, json=body)
            except httpx.HTTPStatusError as exc:
                results.append(
                    PushResult(portal_sku=mapping.portal_sku, quantity=qty, ok=False, message=exc.response.text[:500])
                )
                continue
            ok = data.get("status") == "ACCEPTED"
            results.append(
                PushResult(
                    portal_sku=mapping.portal_sku,
                    quantity=qty,
                    ok=ok,
                    message="" if ok else str(data.get("issues"))[:500],
                )
            )
            await asyncio.sleep(self.listings_delay)
        return results
