"""Order flow (resolve -> analyze -> push) and /api/v1 endpoints against a mocked dealer portal."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db import Database
from app.main import app
from app.models import ChannelOrder, OrderLine, ProcessOrderIn
from app.order_flow import OrderFlow, process_request_from_channel_order
from app.portal_auth import PortalAuth
from app.vagmine import FlowError, VagmineClient

PROCESS = {
    "order_reference_no": "TEST-0001",
    "customer": {"external_customer_id": "amazon:9000000000", "name": "Test Customer", "phone": "9000000000"},
    "items": [{"part_no": "PSFA001B", "quantity": 1}],
}


class FakePortal:
    """Mock dealer portal: records calls, replies can be overridden per path."""

    def __init__(self, **overrides):
        self.calls = []
        self.replies = {
            "/api/v1/auth/login": httpx.Response(200, json={"access_token": "T", "expires_in": 3600}),
            "/api/v1/PUSH_ORDER/customer/resolve": httpx.Response(
                200, json={"data": {"customer_account_id": 2564, "order_for_user_id": 4}}
            ),
            "/api/v1/PUSH_ORDER/analyze": httpx.Response(
                200, json={"success": True, "items": [{"part_no": "PSFA001B", "available": True}]}
            ),
            "/api/v1/PUSH_ORDER": httpx.Response(201, json={"data": {"order_id": 98765}}),
            **overrides,
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.url.path, body))
        return self.replies.get(request.url.path, httpx.Response(404, json={"detail": "not found"}))

    def paths(self):
        return [path for path, _ in self.calls if path != "/api/v1/auth/login"]

    def body(self, path):
        return next(body for p, body in self.calls if p == path)


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "database_path": str(tmp_path / "t.db"),
        "portal_base_url": "https://portal.test",
        "portal_username": "erp",
        "portal_password": "pw",
        "portal_write_enabled": True,
        "portal_home_branch_dealer_id": 23,
        "portal_source_branch_dealer_id": 23,
        **overrides,
    }
    return Settings(_env_file=None, **values)


def make_flow(settings: Settings, portal: FakePortal) -> OrderFlow:
    db = Database(settings.database_path)
    db.init()
    http = httpx.AsyncClient(transport=httpx.MockTransport(portal))
    return OrderFlow(settings, db, VagmineClient(settings, PortalAuth(settings, http)))


def process(flow, data=PROCESS):
    return asyncio.run(flow.process(ProcessOrderIn(**data)))


def test_process_runs_all_steps_and_saves_record(tmp_path):
    portal = FakePortal()
    flow = make_flow(make_settings(tmp_path), portal)

    record = process(flow)

    assert portal.paths() == ["/api/v1/PUSH_ORDER/customer/resolve", "/api/v1/PUSH_ORDER/analyze", "/api/v1/PUSH_ORDER"]
    assert portal.body("/api/v1/PUSH_ORDER/customer/resolve")["shopify_customer_id"] == "amazon:9000000000"
    assert portal.body("/api/v1/PUSH_ORDER") == {
        "order_reference_no": "TEST-0001",
        "source_branch_dealer_id": 23,
        "items": [{"part_no": "PSFA001B", "quantity": 1}],
        "order_for_user_id": 4,  # taken from the resolve reply
        "customer_account_id": 2564,
    }
    assert record["status"] == "pushed"
    assert record["portal_order_id"] == "98765"
    assert set(record["steps"]) == {"resolve", "analyze", "push_order"}


def test_same_reference_is_never_pushed_twice(tmp_path):
    portal = FakePortal()
    flow = make_flow(make_settings(tmp_path), portal)
    process(flow)

    with pytest.raises(FlowError) as exc:
        process(flow)

    assert exc.value.status_code == 409
    assert exc.value.details["portal_order_id"] == "98765"
    assert portal.paths().count("/api/v1/PUSH_ORDER") == 1


def test_unavailable_items_stop_before_push(tmp_path):
    portal = FakePortal(**{
        "/api/v1/PUSH_ORDER/analyze": httpx.Response(
            200, json={"items": [{"part_no": "PSFA001B", "status": "OUT_OF_STOCK"}]}
        )
    })
    flow = make_flow(make_settings(tmp_path), portal)

    with pytest.raises(FlowError) as exc:
        process(flow)

    assert (exc.value.status_code, exc.value.code) == (422, "items_unavailable")
    assert "/api/v1/PUSH_ORDER" not in portal.paths()
    assert flow.db.get_portal_order("TEST-0001")["status"] == "analyze_failed"


def test_failed_order_can_be_retried(tmp_path):
    portal = FakePortal(**{"/api/v1/PUSH_ORDER": httpx.Response(500, text="boom")})
    flow = make_flow(make_settings(tmp_path), portal)
    with pytest.raises(FlowError) as exc:
        process(flow)
    assert (exc.value.status_code, exc.value.code) == (502, "portal_error")
    assert flow.db.get_portal_order("TEST-0001")["status"] == "push_order_failed"

    portal.replies["/api/v1/PUSH_ORDER"] = httpx.Response(201, json={"order_id": 1})
    assert process(flow)["status"] == "pushed"


def test_portal_rejection_is_returned_with_portal_reply(tmp_path):
    portal = FakePortal(**{
        "/api/v1/PUSH_ORDER/customer/resolve": httpx.Response(400, json={"detail": "phone invalid"})
    })
    flow = make_flow(make_settings(tmp_path), portal)

    with pytest.raises(FlowError) as exc:
        process(flow)

    assert exc.value.to_dict()["error"] == {
        "code": "portal_rejected",
        "message": "dealer portal rejected the request",
        "step": "resolve",
        "portal_status": 400,
        "portal_response": {"detail": "phone invalid"},
    }


def test_writes_disabled_blocks_before_any_portal_call(tmp_path):
    portal = FakePortal()
    flow = make_flow(make_settings(tmp_path, portal_write_enabled=False), portal)

    with pytest.raises(FlowError) as exc:
        process(flow)

    assert exc.value.status_code == 403
    assert portal.calls == []


def test_marketplace_orders_map_to_customers():
    flipkart = ChannelOrder(
        channel="flipkart", channel_order_id="OD1", status="APPROVED",
        shipping_address={"firstName": "Ravi", "lastName": "K", "addressLine1": "1 Main Rd", "city": "Salem",
                          "state": "Tamil Nadu", "pincode": "636001", "contactNumber": "9111111111"},
        lines=[OrderLine(channel_sku="FK-1", sku="PSFA001B", quantity=2)],
    )
    amazon = ChannelOrder(channel="amazon", channel_order_id="403-1", status="Unshipped",
                          lines=[OrderLine(channel_sku="AMZ-1", sku="PSFA001B", quantity=1)])

    fk, amz = process_request_from_channel_order(flipkart), process_request_from_channel_order(amazon)

    assert fk.order_reference_no == "FLIPKART-OD1"
    assert fk.customer.external_customer_id == "flipkart:9111111111"
    assert fk.customer.name == "Ravi K"
    assert fk.customer.address == "1 Main Rd, Salem, Tamil Nadu, 636001"
    assert fk.items[0].part_no == "PSFA001B" and fk.items[0].quantity == 2
    assert amz.customer.external_customer_id == "amazon:marketplace"


# --- HTTP API -----------------------------------------------------------------


@pytest.fixture
def api(tmp_path, monkeypatch):
    for key, value in {
        "DATABASE_PATH": str(tmp_path / "api.db"), "SCHEDULER_ENABLED": "false", "PORTAL_MODE": "local",
        "FLIPKART_ENABLED": "false", "AMAZON_ENABLED": "false", "ADMIN_TOKEN": "adm",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    portal = FakePortal()
    with TestClient(app) as client:
        app.state.order_flow = make_flow(make_settings(tmp_path, database_path=str(tmp_path / "api.db")), portal)
        client.headers["X-Admin-Token"] = "adm"
        yield client, portal
    get_settings.cache_clear()


def test_api_process_then_duplicate_then_get(api):
    client, _ = api

    created = client.post("/api/v1/orders/process", json=PROCESS)
    duplicate = client.post("/api/v1/orders/process", json=PROCESS)
    fetched = client.get("/api/v1/orders/TEST-0001")
    listed = client.get("/api/v1/orders", params={"status": "pushed"})

    assert created.status_code == 201 and created.json()["status"] == "pushed"
    assert duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "duplicate_order"
    assert fetched.status_code == 200 and fetched.json()["portal_order_id"] == "98765"
    assert [o["order_reference_no"] for o in listed.json()] == ["TEST-0001"]


def test_api_error_shapes(api):
    client, _ = api

    missing = client.get("/api/v1/orders/NOPE")
    invalid = client.post("/api/v1/orders", json={"order_reference_no": "X", "items": []})
    track = client.get("/api/v1/orders/TEST-0001/track")
    no_token = client.get("/api/v1/orders", headers={"X-Admin-Token": "wrong"})

    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "order_not_found")
    assert (invalid.status_code, invalid.json()["error"]["code"]) == (422, "validation_error")
    assert (track.status_code, track.json()["error"]["code"]) == (501, "tracking_not_configured")
    assert (no_token.status_code, no_token.json()["error"]["code"]) == (401, "unauthorized")


def test_api_analyze_reports_can_push(api):
    client, _ = api
    resp = client.post("/api/v1/orders/analyze", json={
        "order_reference_no": "TEST-0002", "items": [{"part_no": "PSFA001B", "quantity": 1}],
        "customer_account_id": 2564, "order_for_user_id": 4,
    })
    assert resp.status_code == 200
    assert resp.json()["can_push"] is True
