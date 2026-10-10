"""Command-line helpers.

    python -m app.cli import-skus sku_map.csv
    python -m app.cli check-channels      # read-only: log in to Amazon/Flipkart and count recent orders
"""

import argparse
import asyncio
import csv
from datetime import datetime, timedelta, timezone

import httpx

from app.channels.amazon import AmazonChannel
from app.channels.flipkart import OPEN_STATES, FlipkartChannel, _fk_time
from app.config import get_settings
from app.db import Database
from app.models import SkuMapping


def _failure(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}: {exc.response.text[:400]}"
    return f"{type(exc).__name__}: {exc}"


async def check_channels(days: int = 7) -> None:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    async with httpx.AsyncClient(timeout=30) as http:
        if settings.amazon_client_id:
            amazon = AmazonChannel(settings, http)
            try:
                await amazon._access_token()
                print("Amazon  login: OK")
                data = await amazon._call("GET", "/orders/v0/orders", params={
                    "MarketplaceIds": settings.amazon_marketplace_id,
                    "CreatedAfter": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
                orders = (data.get("payload") or {}).get("Orders") or []
                by_channel = {}
                for o in orders:
                    by_channel[o.get("FulfillmentChannel", "?")] = by_channel.get(o.get("FulfillmentChannel", "?"), 0) + 1
                more = " (more pages)" if (data.get("payload") or {}).get("NextToken") else ""
                print(f"Amazon  orders API: OK, {len(orders)} orders in last {days} days{more} {by_channel}")
            except Exception as exc:
                print(f"Amazon  FAILED: {_failure(exc)}")
        else:
            print("Amazon  skipped: no credentials in .env")

        if settings.flipkart_app_id:
            flipkart = FlipkartChannel(settings, http)
            try:
                await flipkart._access_token()
                print("Flipkart login: OK")
                data = await flipkart._call("POST", "/sellers/v3/shipments/filter/", json={
                    "filter": {"type": "preDispatch", "states": OPEN_STATES,
                               "orderDate": {"from": _fk_time(since), "to": _fk_time(now)}},
                    "pagination": {"pageSize": 20},
                })
                more = " (more pages)" if data.get("hasMore") else ""
                print(f"Flipkart orders API: OK, {len(data.get('shipments') or [])} open shipments in last {days} days{more}")
            except Exception as exc:
                print(f"Flipkart FAILED: {_failure(exc)}")
        else:
            print("Flipkart skipped: no credentials in .env")


def import_skus(path: str) -> int:
    with open(path, newline="", encoding="utf-8-sig") as f:
        mappings = [
            SkuMapping(
                portal_sku=row["portal_sku"].strip(),
                amazon_sku=(row.get("amazon_sku") or "").strip() or None,
                flipkart_sku=(row.get("flipkart_sku") or "").strip() or None,
                flipkart_fsn=(row.get("flipkart_fsn") or "").strip() or None,
                active=(row.get("active") or "1").strip().lower() not in ("0", "false", "no"),
            )
            for row in csv.DictReader(f)
            if (row.get("portal_sku") or "").strip()
        ]
    db = Database(get_settings().database_path)
    db.init()
    return db.upsert_mappings(mappings)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    skus = sub.add_parser("import-skus", help="load/update the SKU map from a CSV file")
    skus.add_argument("csv_path")
    check = sub.add_parser("check-channels", help="read-only login + recent-order check for Amazon/Flipkart")
    check.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    if args.command == "import-skus":
        print(f"Saved {import_skus(args.csv_path)} SKU mappings")
    elif args.command == "check-channels":
        asyncio.run(check_channels(args.days))


if __name__ == "__main__":
    main()
