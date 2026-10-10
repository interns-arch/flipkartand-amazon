# CarTrends Marketplace Sync

Connects Amazon.in and Flipkart to the CarTrends dealer portal:

- **Orders:** every 5 minutes, new seller-fulfilled orders from Amazon and Flipkart are created in the dealer portal. Each order is created only once.
- **Stock:** the dealer portal is the source of truth. After every sale, and whenever the portal reports a stock change, the same quantity is pushed to both marketplaces. A full re-sync of all SKUs also runs every 6 hours.

```
Amazon SP-API ─┐                          ┌─> Dealer portal (orders in, stock out)
               ├──> this service ─────────┤
Flipkart API ──┘    (FastAPI + SQLite)    └─> SKU map, order log, stock-push log
```

## Setup

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env        # then fill in .env
.venv\Scripts\python -m pytest -q
.venv\Scripts\uvicorn app.main:app --port 8000
```

API docs: http://localhost:8000/docs

## What we need, and from whom

| Item | Where it comes from | Who |
|---|---|---|
| Flipkart App ID + Secret | Seller Hub → Developer Access → Self Access (status must be **Active**) | Manish |
| Flipkart location ID | Seller Hub → pickup/warehouse locations | Manish |
| Amazon LWA client ID + secret | Developer Central → LWA credentials → View | Manish |
| Amazon refresh token | Developer Central → Edit App ▾ → Authorize | Manish |
| Amazon seller ID (merchant token) | Seller Central → Settings → Account Info | Manish |
| Amazon app roles: Inventory and Order Tracking, Product Listing (+ Direct-to-Consumer Shipping for buyer addresses) | Developer Central → Edit App | Manish / Anik sir |
| Dealer portal API (create order, read stock, stock-change webhook) | Portal developer | Anik sir |
| SKU map: portal SKU ↔ Amazon SKU ↔ Flipkart SKU + FSN | Exports from the three systems | Team |

## Go-live checklist

1. Fill `.env`. Keep `DRY_RUN=true`.
2. Load the SKU map with `python -m app.cli import-skus sku_map.csv` (see `sku_map.example.csv`).
3. Turn on one channel (`FLIPKART_ENABLED=true`). Call `POST /sync/orders` and check that `GET /orders/recent` shows real orders.
4. Call `POST /sync/stock` and check that `GET /stock/pushes` shows the quantities you expect.
5. Set `DRY_RUN=false`, run `POST /sync/stock` for 1–2 test SKUs, then verify them in Seller Hub / Seller Central.
6. Switch to `PORTAL_MODE=rest` once the portal API is ready (contract in `app/portal.py`).
7. Repeat steps 3–5 for Amazon.

## Dealer portal order flow (Vagmine)

Each order goes through four steps on the dealer portal (`https://vagmine.vagminetech.com`):

**resolve customer → analyze products → push order → track**

- Frontend API reference: [docs/API.md](docs/API.md)
- Test page with live Send buttons and every success/failure response: http://localhost:8000/tester
- Safety switch: with `PORTAL_WRITE_ENABLED=false` (the default), nothing is created on the portal. Analyze is read-only and always allowed.
- `PORTAL_MODE=rest` sends Amazon/Flipkart orders through the same flow, using the reference `AMAZON-<id>` / `FLIPKART-<id>`.

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | Status, enabled channels, last sync results |
| POST | `/sync/orders` | admin | Pull orders now |
| POST | `/sync/stock` | admin | Push stock now (`{"skus": [...]}` or all) |
| POST | `/webhooks/portal/stock-changed` | `X-Webhook-Secret` | Portal reports changed SKUs |
| GET/PUT | `/sku-map` | admin | View or update SKU mappings |
| GET | `/orders/recent` | admin | Order log, including failures and the reason |
| GET | `/stock/pushes` | admin | Stock push log |
| PUT | `/local-portal/stock` | admin | Test only: set stock in the built-in portal |

Admin endpoints need the header `X-Admin-Token: <ADMIN_TOKEN>`.

## Notes

- **FBA / Flipkart-fulfilled stock is not synced.** It sits in the marketplace's warehouse, and only their systems manage it.
- **Unmapped SKUs:** if an order contains a SKU that is not in the SKU map, it is logged as `error` and retried on every poll until the mapping is added.
- **Missing stock:** a SKU whose stock cannot be read from the portal is never pushed. This prevents sending a wrong 0.
- **`STOCK_BUFFER`** holds back units from the marketplaces to reduce overselling between polls.
