import asyncio
import logging
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from app.channels import Channel
from app.config import Settings
from app.db import Database, now_iso
from app.models import ChannelOrder
from app.portal import DealerPortal

log = logging.getLogger(__name__)


class UnmappedSkuError(Exception):
    pass


class SyncService:
    """Moves orders marketplace -> dealer portal and stock dealer portal -> marketplaces."""

    def __init__(self, settings: Settings, db: Database, portal: DealerPortal, channels: dict[str, Channel]):
        self.settings = settings
        self.db = db
        self.portal = portal
        self.channels = channels
        self._order_lock = asyncio.Lock()
        self._stock_lock = asyncio.Lock()
        self.status: dict = {"last_order_pull": None, "last_stock_push": None}

    def _map_lines(self, order: ChannelOrder) -> None:
        missing = []
        for line in order.lines:
            mapping = self.db.find_mapping(order.channel, line.channel_sku)
            if mapping is None:
                if getattr(self.portal, "requires_sku_mapping", True):
                    missing.append(line.channel_sku)
            else:
                line.sku = mapping.portal_sku
        if missing:
            raise UnmappedSkuError(f"no SKU mapping for {order.channel} SKU(s): {', '.join(missing)}")

    async def pull_orders(self) -> dict:
        summary: dict = {}
        touched: set[str] = set()
        async with self._order_lock:
            since = datetime.now(timezone.utc) - timedelta(hours=self.settings.lookback_hours)
            for name, channel in self.channels.items():
                try:
                    orders = await channel.fetch_orders(
                        since, is_known=lambda order_id, n=name: self.db.order_created(n, order_id)
                    )
                except Exception as exc:
                    log.exception("Fetching %s orders failed", name)
                    summary[name] = {"error": str(exc)}
                    continue

                created = failed = 0
                for order in orders:
                    if self.db.order_created(name, order.channel_order_id):
                        continue
                    try:
                        self._map_lines(order)
                        portal_order_id = await self.portal.create_order(order)
                    except Exception as exc:
                        # Left as "error" so the next poll retries it (e.g. once the SKU is mapped)
                        log.warning("%s order %s not created: %s", name, order.channel_order_id, exc)
                        self.db.record_order(order, status="error", message=str(exc)[:1000])
                        failed += 1
                        continue
                    self.db.record_order(order, status="created", portal_order_id=portal_order_id)
                    created += 1
                    if order.seller_fulfilled:
                        touched.update(line.sku for line in order.lines if line.sku)
                    log.info("%s order %s -> portal %s", name, order.channel_order_id, portal_order_id)
                summary[name] = {"fetched": len(orders), "created": created, "failed": failed}
            self.status["last_order_pull"] = {"at": now_iso(), "summary": summary}

        # A sale on one marketplace changes portal stock, so refresh every marketplace for those SKUs
        if touched:
            summary["stock"] = await self.push_stock(touched)
        return summary

    async def push_stock(self, skus: Iterable[str] | None = None) -> dict:
        """Send current portal stock to every marketplace. skus=None means all mapped SKUs."""
        async with self._stock_lock:
            mappings = self.db.list_mappings(skus)
            result: dict = {"dry_run": self.settings.dry_run, "skus": len(mappings)}
            if not mappings:
                return result

            stock = await self.portal.get_stock([m.portal_sku for m in mappings])
            # Never push a quantity we could not read from the portal
            unknown = [m.portal_sku for m in mappings if m.portal_sku not in stock]
            if unknown:
                result["no_portal_stock"] = unknown

            for name, channel in self.channels.items():
                items = [
                    (m, max(0, stock[m.portal_sku] - self.settings.stock_buffer))
                    for m in mappings
                    if m.channel_sku(name) and m.portal_sku in stock
                ]
                if not items:
                    continue
                if self.settings.dry_run:
                    for mapping, qty in items:
                        self.db.log_push(name, mapping.portal_sku, qty, ok=True, message="dry-run, not sent")
                    result[name] = {"would_push": len(items)}
                    continue
                try:
                    outcomes = await channel.push_stock(items)
                except Exception as exc:
                    log.exception("Pushing stock to %s failed", name)
                    result[name] = {"error": str(exc)}
                    continue
                for outcome in outcomes:
                    self.db.log_push(name, outcome.portal_sku, outcome.quantity, outcome.ok, outcome.message)
                failed = [o.portal_sku for o in outcomes if not o.ok]
                result[name] = {"pushed": len(outcomes) - len(failed), "failed": failed}

            self.status["last_stock_push"] = {"at": now_iso(), "result": result}
            return result
