import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("FLIPKART_ENABLED", "false")
    monkeypatch.setenv("AMAZON_ENABLED", "false")
    monkeypatch.setenv("PORTAL_MODE", "local")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    get_settings.cache_clear()
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def test_health_returns_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "cartrends-marketplace-sync"
    assert body["uptime_seconds"] >= 0
    assert body["dry_run"] is True


def test_admin_endpoints_require_token(client):
    assert client.get("/sku-map").status_code == 401
    assert client.get("/sku-map", headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert client.get("/sku-map", headers={"X-Admin-Token": "test-admin"}).status_code == 200


def test_sku_map_round_trip(client):
    headers = {"X-Admin-Token": "test-admin"}
    saved = client.put("/sku-map", headers=headers, json=[{"portal_sku": "BRK-001", "amazon_sku": "AMZ-BRK-001"}])
    assert saved.json() == {"saved": 1}
    assert client.get("/sku-map", headers=headers).json()[0]["amazon_sku"] == "AMZ-BRK-001"


def test_webhook_rejects_without_secret(client):
    assert client.post("/webhooks/portal/stock-changed", json={"skus": ["BRK-001"]}).status_code == 401
