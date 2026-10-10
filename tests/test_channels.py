"""Connector tests against mocked Flipkart / Amazon HTTP APIs (no network)."""

import asyncio
import json
from datetime import datetime, timezone

import httpx

from app.channels.amazon import AmazonChannel
from app.channels.flipkart import FlipkartChannel
from app.config import Settings
from app.models import SkuMapping

MAPPING = SkuMapping(portal_sku="BRK-001", amazon_sku="AMZ-BRK-001", flipkart_sku="FK-BRK-001", flipkart_fsn="FSN1")


def run_with(handler, make_channel, action):
    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await action(make_channel(http))

    return asyncio.run(main())


def test_flipkart_orders_and_inventory():
    settings = Settings(
        _env_file=None, flipkart_app_id="app", flipkart_app_secret="secret", flipkart_location_id="LOC1"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/oauth-service/oauth/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer tok"
        if path == "/sellers/v3/shipments/filter/":
            return httpx.Response(
                200,
                json={
                    "hasMore": False,
                    "shipments": [
                        {
                            "shipmentId": "S1",
                            "orderItems": [
                                {
                                    "orderId": "OD1",
                                    "orderItemId": "I1",
                                    "sku": "FK-BRK-001",
                                    "quantity": 2,
                                    "status": "APPROVED",
                                    "priceComponents": {"sellingPrice": 500},
                                }
                            ],
                        }
                    ],
                },
            )
        if path == "/sellers/v3/shipments/S1":
            return httpx.Response(200, json={"shipments": [{"shipmentId": "S1", "deliveryAddress": {"city": "Delhi"}}]})
        if path == "/sellers/listings/v3/update/inventory":
            assert json.loads(request.content) == {
                "FK-BRK-001": {"product_id": "FSN1", "locations": [{"id": "LOC1", "inventory": 7}]}
            }
            return httpx.Response(200, json={"FK-BRK-001": {"status": "SUCCESS"}})
        return httpx.Response(404)

    async def action(channel):
        orders = await channel.fetch_orders(datetime.now(timezone.utc), lambda _: False)
        return orders, await channel.push_stock([(MAPPING, 7)])

    orders, results = run_with(handler, lambda http: FlipkartChannel(settings, http), action)

    assert len(orders) == 1
    assert orders[0].channel_order_id == "OD1"
    assert orders[0].lines[0].channel_sku == "FK-BRK-001"
    assert orders[0].lines[0].quantity == 2
    assert orders[0].total == 500
    assert orders[0].shipping_address == {"city": "Delhi"}
    assert results[0].ok


def test_amazon_orders_and_listing_quantity():
    settings = Settings(
        _env_file=None,
        amazon_client_id="cid",
        amazon_client_secret="csecret",
        amazon_refresh_token="rtoken",
        amazon_seller_id="SELLER1",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "api.amazon.com":
            return httpx.Response(200, json={"access_token": "atok", "expires_in": 3600})
        assert request.headers["x-amz-access-token"] == "atok"
        if path == "/orders/v0/orders":
            assert request.url.params["FulfillmentChannels"] == "MFN"
            return httpx.Response(
                200,
                json={
                    "payload": {
                        "Orders": [
                            {
                                "AmazonOrderId": "403-1",
                                "OrderStatus": "Unshipped",
                                "FulfillmentChannel": "MFN",
                                "OrderTotal": {"Amount": "999.00", "CurrencyCode": "INR"},
                            },
                            {"AmazonOrderId": "403-OLD", "OrderStatus": "Unshipped"},
                        ]
                    }
                },
            )
        if path == "/orders/v0/orders/403-1/orderItems":
            return httpx.Response(
                200,
                json={
                    "payload": {
                        "OrderItems": [
                            {"SellerSKU": "AMZ-BRK-001", "QuantityOrdered": 1, "ItemPrice": {"Amount": "999.00"}}
                        ]
                    }
                },
            )
        if path == "/listings/2021-08-01/items/SELLER1/AMZ-BRK-001":
            assert request.method == "PATCH"
            body = json.loads(request.content)
            assert body["patches"][0]["value"][0]["quantity"] == 7
            return httpx.Response(200, json={"sku": "AMZ-BRK-001", "status": "ACCEPTED"})
        return httpx.Response(404)

    def make_channel(http):
        channel = AmazonChannel(settings, http)
        channel.order_items_delay = channel.listings_delay = 0
        return channel

    async def action(channel):
        orders = await channel.fetch_orders(datetime.now(timezone.utc), lambda oid: oid == "403-OLD")
        return orders, await channel.push_stock([(MAPPING, 7)])

    orders, results = run_with(handler, make_channel, action)

    assert [o.channel_order_id for o in orders] == ["403-1"]
    assert orders[0].total == 999.0
    assert orders[0].lines[0].channel_sku == "AMZ-BRK-001"
    assert results[0].ok
