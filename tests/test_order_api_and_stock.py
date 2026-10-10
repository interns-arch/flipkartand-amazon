"""Order forwarding to ORDER_API_URL and the POST /api/v1/stock endpoint."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db import Database
from app.main import app
from app.models import ChannelOrder, OrderLine, SkuMapping
from app.portal import OrderApiPortal
from app.sync import SyncService
from tests.test_sync import FakeChannel


def make(tmp_path, handler, **overrides):
    values = {"database_path": str(tmp_path / "t.db"), "portal_mode": "api", "dry_run": False,
              "order_api_url": "https://erp.test/orders", "order_api_token": "tok", **overrides}
    settings = Settings(_env_file=None, **values)
    db = Database(settings.database_path)
    db.init()
    portal = OrderApiPortal(settings, db, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return settings, db, portal


def order(sku="AMZ-1", qty=2):
    return ChannelOrder(channel="amazon", channel_order_id="403-1", status="Unshipped",
                        lines=[OrderLine(channel_sku=sku, quantity=qty)])


def test_order_is_posted_to_order_api_and_stock_reduced(tmp_path):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(201, json={"id": "ERP-77"})

    settings, db, portal = make(tmp_path, handler)
    db.upsert_mappings([SkuMapping(portal_sku="PSFA001B", amazon_sku="AMZ-1")])
    db.set_synced_stock({"PSFA001B": 10})
    amazon = FakeChannel("amazon", [order()])
    service = SyncService(settings, db, portal, {"amazon": amazon})

    summary = asyncio.run(service.pull_orders())

    assert summary["amazon"]["created"] == 1
    request = sent[0]
    assert request.headers["Authorization"] == "Bearer tok"
    assert request.headers["Idempotency-Key"] == "amazon-403-1"
    body = json.loads(request.content)
    assert body["channel_order_id"] == "403-1"
    assert body["lines"][0] == {"channel_sku": "AMZ-1", "quantity": 2, "sku": "PSFA001B",
                                "line_total": None, "title": None, "channel_line_id": None}
    assert db.get_synced_stock(["PSFA001B"]) == {"PSFA001B": 8}
    assert amazon.pushed == [("PSFA001B", 8)]  # the sale is reflected on the marketplace


def test_unmapped_sku_is_still_forwarded(tmp_path):
    sent = []
    settings, db, portal = make(tmp_path, lambda r: sent.append(r) or httpx.Response(200))
    service = SyncService(settings, db, portal, {"amazon": FakeChannel("amazon", [order(sku="UNKNOWN")])})

    summary = asyncio.run(service.pull_orders())

    assert summary["amazon"]["created"] == 1
    assert json.loads(sent[0].content)["lines"][0]["sku"] is None


def test_missing_url_keeps_order_for_retry(tmp_path):
    settings, db, portal = make(tmp_path, lambda r: httpx.Response(200), order_api_url="")
    service = SyncService(settings, db, portal, {"amazon": FakeChannel("amazon", [order()])})

    summary = asyncio.run(service.pull_orders())

    assert summary["amazon"]["failed"] == 1
    assert "ORDER_API_URL" in db.recent_orders()[0]["message"]


@pytest.mark.parametrize("status,ok", [(200, True), (409, True), (400, False), (500, False)])
def test_order_api_status_handling(tmp_path, status, ok):
    _, _, portal = make(tmp_path, lambda r: httpx.Response(status, text="x"))
    if ok:
        assert asyncio.run(portal.create_order(order())) == "amazon-403-1"
    else:
        with pytest.raises(RuntimeError, match=f"HTTP {status}"):
            asyncio.run(portal.create_order(order()))


# --- POST /api/v1/stock --------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    for key, value in {"DATABASE_PATH": str(tmp_path / "api.db"), "SCHEDULER_ENABLED": "false",
                       "PORTAL_MODE": "api", "FLIPKART_ENABLED": "false", "AMAZON_ENABLED": "false",
                       "ADMIN_TOKEN": "adm"}.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = "adm"
        yield c
    get_settings.cache_clear()


def test_stock_api_saves_and_reports_unmapped(client):
    client.put("/sku-map", json=[{"portal_sku": "PSFA001B", "amazon_sku": "AMZ-1"}])

    resp = client.post("/api/v1/stock", json={"items": [
        {"sku": "PSFA001B", "quantity": 5}, {"sku": "NEW-PART", "quantity": 3},
    ]})

    assert resp.status_code == 202
    assert resp.json() == {"accepted": 2, "pushing": ["PSFA001B"], "unmapped": ["NEW-PART"], "dry_run": True}
    assert app.state.db.get_synced_stock(["PSFA001B", "NEW-PART"]) == {"PSFA001B": 5, "NEW-PART": 3}


def test_stock_api_validation_and_auth(client):
    negative = client.post("/api/v1/stock", json={"items": [{"sku": "A", "quantity": -1}]})
    empty = client.post("/api/v1/stock", json={"items": []})
    no_token = client.post("/api/v1/stock", json={"items": [{"sku": "A", "quantity": 1}]},
                           headers={"X-Admin-Token": "bad"})

    assert (negative.status_code, negative.json()["error"]["code"]) == (422, "validation_error")
    assert empty.status_code == 422
    assert no_token.status_code == 401


def test_stock_api_needs_api_mode(tmp_path, monkeypatch, client):
    app.state.settings = Settings(_env_file=None, portal_mode="local", admin_token="adm")
    resp = client.post("/api/v1/stock", json={"items": [{"sku": "A", "quantity": 1}]})
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "stock_api_disabled")
