import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.models import ChannelOrder, SkuMapping

CHANNEL_COLUMNS = {"amazon": "amazon_sku", "flipkart": "flipkart_sku"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sku_map (
    portal_sku   TEXT PRIMARY KEY,
    amazon_sku   TEXT,
    flipkart_sku TEXT,
    flipkart_fsn TEXT,
    active       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_sku_map_amazon ON sku_map(amazon_sku);
CREATE INDEX IF NOT EXISTS idx_sku_map_flipkart ON sku_map(flipkart_sku);

CREATE TABLE IF NOT EXISTS channel_orders (
    channel          TEXT NOT NULL,
    channel_order_id TEXT NOT NULL,
    status           TEXT NOT NULL,  -- created | error
    portal_order_id  TEXT,
    message          TEXT,
    payload          TEXT,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (channel, channel_order_id)
);

CREATE TABLE IF NOT EXISTS stock_pushes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel    TEXT NOT NULL,
    portal_sku TEXT NOT NULL,
    quantity   INTEGER NOT NULL,
    ok         INTEGER NOT NULL,
    message    TEXT,
    at         TEXT NOT NULL
);

-- Orders sent through the dealer-portal flow (resolve -> analyze -> push)
CREATE TABLE IF NOT EXISTS portal_orders (
    order_reference_no  TEXT PRIMARY KEY,
    status              TEXT NOT NULL,  -- processing | resolve_failed | analyze_failed | push_order_failed | pushed
    customer_account_id INTEGER,
    portal_order_id     TEXT,
    request             TEXT NOT NULL,
    steps               TEXT NOT NULL,
    error               TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

-- Stock received on POST /api/v1/stock (PORTAL_MODE=api)
CREATE TABLE IF NOT EXISTS synced_stock (
    sku        TEXT PRIMARY KEY,
    quantity   INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

-- Built-in stand-in for the dealer portal (PORTAL_MODE=local)
CREATE TABLE IF NOT EXISTS local_stock (
    sku      TEXT PRIMARY KEY,
    quantity INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS local_orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    channel          TEXT NOT NULL,
    channel_order_id TEXT NOT NULL,
    payload          TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _placeholders(values: list) -> str:
    return ",".join("?" * len(values))


def _row_to_mapping(row: sqlite3.Row) -> SkuMapping:
    return SkuMapping(
        portal_sku=row["portal_sku"],
        amazon_sku=row["amazon_sku"],
        flipkart_sku=row["flipkart_sku"],
        flipkart_fsn=row["flipkart_fsn"],
        active=bool(row["active"]),
    )


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    # --- SKU map -----------------------------------------------------------

    def upsert_mappings(self, mappings: Iterable[SkuMapping]) -> int:
        rows = [
            (m.portal_sku, m.amazon_sku, m.flipkart_sku, m.flipkart_fsn, int(m.active))
            for m in mappings
        ]
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO sku_map (portal_sku, amazon_sku, flipkart_sku, flipkart_fsn, active)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(portal_sku) DO UPDATE SET
                    amazon_sku = excluded.amazon_sku,
                    flipkart_sku = excluded.flipkart_sku,
                    flipkart_fsn = excluded.flipkart_fsn,
                    active = excluded.active
                """,
                rows,
            )
        return len(rows)

    def list_mappings(self, skus: Iterable[str] | None = None, active_only: bool = True) -> list[SkuMapping]:
        query, params = "SELECT * FROM sku_map WHERE 1=1", []
        if active_only:
            query += " AND active = 1"
        if skus is not None:
            skus = list(skus)
            if not skus:
                return []
            query += f" AND portal_sku IN ({_placeholders(skus)})"
            params += skus
        with self.connect() as conn:
            return [_row_to_mapping(r) for r in conn.execute(query + " ORDER BY portal_sku", params)]

    def find_mapping(self, channel: str, channel_sku: str) -> SkuMapping | None:
        column = CHANNEL_COLUMNS[channel]
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT * FROM sku_map WHERE {column} = ? AND active = 1", (channel_sku,)
            ).fetchone()
        return _row_to_mapping(row) if row else None

    # --- Orders ------------------------------------------------------------

    def order_created(self, channel: str, channel_order_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM channel_orders WHERE channel = ? AND channel_order_id = ? AND status = 'created'",
                (channel, channel_order_id),
            ).fetchone()
        return row is not None

    def record_order(
        self,
        order: ChannelOrder,
        status: str,
        portal_order_id: str | None = None,
        message: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO channel_orders
                    (channel, channel_order_id, status, portal_order_id, message, payload, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel, channel_order_id) DO UPDATE SET
                    status = excluded.status,
                    portal_order_id = excluded.portal_order_id,
                    message = excluded.message,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    order.channel,
                    order.channel_order_id,
                    status,
                    portal_order_id,
                    message,
                    order.model_dump_json(),
                    now_iso(),
                ),
            )

    def recent_orders(self, limit: int = 50) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT channel, channel_order_id, status, portal_order_id, message, updated_at
                FROM channel_orders ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            )
            return [dict(r) for r in rows]

    # --- Stock push log ----------------------------------------------------

    def log_push(self, channel: str, portal_sku: str, quantity: int, ok: bool, message: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO stock_pushes (channel, portal_sku, quantity, ok, message, at) VALUES (?, ?, ?, ?, ?, ?)",
                (channel, portal_sku, quantity, int(ok), message, now_iso()),
            )

    def recent_pushes(self, limit: int = 100) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM stock_pushes ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(r) for r in rows]

    # --- Dealer-portal order flow -------------------------------------------

    def save_portal_order(self, record: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO portal_orders (order_reference_no, status, customer_account_id, portal_order_id,
                                           request, steps, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_reference_no) DO UPDATE SET
                    status = excluded.status,
                    customer_account_id = excluded.customer_account_id,
                    portal_order_id = excluded.portal_order_id,
                    request = excluded.request,
                    steps = excluded.steps,
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                (
                    record["order_reference_no"],
                    record["status"],
                    record.get("customer_account_id"),
                    record.get("portal_order_id"),
                    json.dumps(record.get("request") or {}),
                    json.dumps(record.get("steps") or {}),
                    json.dumps(record["error"]) if record.get("error") else None,
                    record.get("created_at") or now_iso(),
                    now_iso(),
                ),
            )

    def get_portal_order(self, order_reference_no: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM portal_orders WHERE order_reference_no = ?", (order_reference_no,)
            ).fetchone()
        return self._portal_order(row) if row else None

    def list_portal_orders(self, limit: int = 50, status: str | None = None) -> list[dict]:
        query, params = "SELECT * FROM portal_orders", []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        with self.connect() as conn:
            rows = conn.execute(query + " ORDER BY updated_at DESC LIMIT ?", [*params, limit]).fetchall()
        return [self._portal_order(r) for r in rows]

    @staticmethod
    def _portal_order(row: sqlite3.Row) -> dict:
        record = dict(row)
        record["request"] = json.loads(record["request"])
        record["steps"] = json.loads(record["steps"])
        record["error"] = json.loads(record["error"]) if record["error"] else None
        return record

    # --- Synced stock (PORTAL_MODE=api) -------------------------------------

    def set_synced_stock(self, stock: dict[str, int]) -> None:
        at = now_iso()
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO synced_stock (sku, quantity, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(sku) DO UPDATE SET quantity = excluded.quantity, updated_at = excluded.updated_at",
                [(sku, qty, at) for sku, qty in stock.items()],
            )

    def reduce_synced_stock(self, sold: dict[str, int]) -> None:
        """Take sold units off until the next stock update arrives, so a resync can't re-list them."""
        with self.connect() as conn:
            conn.executemany(
                "UPDATE synced_stock SET quantity = MAX(quantity - ?, 0), updated_at = ? WHERE sku = ?",
                [(qty, now_iso(), sku) for sku, qty in sold.items()],
            )

    def get_synced_stock(self, skus: list[str]) -> dict[str, int]:
        if not skus:
            return {}
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT sku, quantity FROM synced_stock WHERE sku IN ({_placeholders(skus)})", skus
            )
            return {r["sku"]: r["quantity"] for r in rows}

    # --- Local portal (PORTAL_MODE=local) ----------------------------------

    def local_set_stock(self, stock: dict[str, int]) -> None:
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO local_stock (sku, quantity) VALUES (?, ?) "
                "ON CONFLICT(sku) DO UPDATE SET quantity = excluded.quantity",
                list(stock.items()),
            )

    def local_get_stock(self, skus: list[str]) -> dict[str, int]:
        if not skus:
            return {}
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT sku, quantity FROM local_stock WHERE sku IN ({_placeholders(skus)})", skus
            )
            return {r["sku"]: r["quantity"] for r in rows}

    def local_create_order(self, order: ChannelOrder) -> str:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO local_orders (channel, channel_order_id, payload, created_at) VALUES (?, ?, ?, ?)",
                (order.channel, order.channel_order_id, order.model_dump_json(), now_iso()),
            )
            if order.seller_fulfilled:
                conn.executemany(
                    "UPDATE local_stock SET quantity = quantity - ? WHERE sku = ?",
                    [(line.quantity, line.sku) for line in order.lines],
                )
            return f"LOCAL-{cur.lastrowid}"
