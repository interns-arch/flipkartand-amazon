import asyncio

from app.config import Settings
from app.db import Database
from app.models import ChannelOrder, OrderLine, PushResult, SkuMapping
from app.portal import LocalPortal
from app.sync import SyncService


class FakeChannel:
    def __init__(self, name, orders=()):
        self.name = name
        self.orders = list(orders)
        self.pushed = []

    async def fetch_orders(self, since, is_known):
        return [o.model_copy(deep=True) for o in self.orders if not is_known(o.channel_order_id)]

    async def push_stock(self, items):
        self.pushed.extend((m.portal_sku, qty) for m, qty in items)
        return [PushResult(portal_sku=m.portal_sku, quantity=qty, ok=True) for m, qty in items]


def make_service(tmp_path, channels, **overrides):
    settings = Settings(_env_file=None, **{"database_path": str(tmp_path / "t.db"), "dry_run": False, **overrides})
    db = Database(settings.database_path)
    db.init()
    db.upsert_mappings(
        [SkuMapping(portal_sku="BRK-001", amazon_sku="AMZ-BRK-001", flipkart_sku="FK-BRK-001", flipkart_fsn="FSN1")]
    )
    db.local_set_stock({"BRK-001": 10})
    return SyncService(settings, db, LocalPortal(db), channels), db


def amazon_order(order_id="403-1", sku="AMZ-BRK-001", qty=2):
    return ChannelOrder(
        channel="amazon",
        channel_order_id=order_id,
        status="Unshipped",
        lines=[OrderLine(channel_sku=sku, quantity=qty)],
    )


def test_order_lands_in_portal_and_stock_syncs_to_all_channels(tmp_path):
    amazon, flipkart = FakeChannel("amazon", [amazon_order()]), FakeChannel("flipkart")
    service, db = make_service(tmp_path, {"amazon": amazon, "flipkart": flipkart})

    summary = asyncio.run(service.pull_orders())

    assert summary["amazon"]["created"] == 1
    assert db.local_get_stock(["BRK-001"]) == {"BRK-001": 8}
    assert amazon.pushed == [("BRK-001", 8)]
    assert flipkart.pushed == [("BRK-001", 8)]


def test_same_order_is_never_created_twice(tmp_path):
    amazon = FakeChannel("amazon", [amazon_order()])
    service, db = make_service(tmp_path, {"amazon": amazon})

    asyncio.run(service.pull_orders())
    second = asyncio.run(service.pull_orders())

    assert second["amazon"] == {"fetched": 0, "created": 0, "failed": 0}
    assert db.local_get_stock(["BRK-001"]) == {"BRK-001": 8}
    assert len(db.recent_orders()) == 1


def test_unmapped_sku_fails_then_retries_once_mapped(tmp_path):
    amazon = FakeChannel("amazon", [amazon_order(sku="AMZ-NEW")])
    service, db = make_service(tmp_path, {"amazon": amazon})

    first = asyncio.run(service.pull_orders())
    assert first["amazon"]["failed"] == 1
    assert db.recent_orders()[0]["status"] == "error"

    db.upsert_mappings([SkuMapping(portal_sku="BRK-002", amazon_sku="AMZ-NEW")])
    db.local_set_stock({"BRK-002": 5})
    second = asyncio.run(service.pull_orders())

    assert second["amazon"]["created"] == 1
    assert db.recent_orders()[0]["status"] == "created"
    assert db.local_get_stock(["BRK-002"]) == {"BRK-002": 3}


def test_dry_run_logs_but_never_pushes(tmp_path):
    flipkart = FakeChannel("flipkart")
    service, db = make_service(tmp_path, {"flipkart": flipkart}, dry_run=True)

    result = asyncio.run(service.push_stock())

    assert result["flipkart"] == {"would_push": 1}
    assert flipkart.pushed == []
    assert db.recent_pushes()[0]["message"].startswith("dry-run")


def test_buffer_applied_and_unknown_stock_never_pushed(tmp_path):
    flipkart = FakeChannel("flipkart")
    service, db = make_service(tmp_path, {"flipkart": flipkart}, stock_buffer=3)
    db.upsert_mappings([SkuMapping(portal_sku="NO-STOCK", flipkart_sku="FK-X", flipkart_fsn="FSN2")])

    result = asyncio.run(service.push_stock())

    assert flipkart.pushed == [("BRK-001", 7)]
    assert result["no_portal_stock"] == ["NO-STOCK"]


def test_fba_order_does_not_reduce_portal_stock(tmp_path):
    order = amazon_order()
    order.seller_fulfilled = False
    service, db = make_service(tmp_path, {"amazon": FakeChannel("amazon", [order])})

    asyncio.run(service.pull_orders())

    assert db.local_get_stock(["BRK-001"]) == {"BRK-001": 10}
