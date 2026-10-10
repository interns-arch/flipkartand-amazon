# CarTrends Marketplace Sync

A small FastAPI service that connects **Amazon.in** and **Flipkart** to our dealer system.

- **Orders in.** Every 5 minutes it fetches new seller-fulfilled orders from Amazon and Flipkart and forwards each one to our order API (`ORDER_API_URL`). Each order is sent only once. Failed sends are retried.
- **Stock out.** Our system sends stock to `POST /api/v1/stock`. The service then updates the same quantities on Amazon and Flipkart. After each sale it re-syncs the sold SKUs, and every 6 hours it re-syncs everything.

```
Amazon SP-API ─┐                                   ┌─> ORDER_API_URL       (new orders)
               ├──> marketplace sync (this repo) ──┤
Flipkart API ──┘    FastAPI + SQLite               └─< POST /api/v1/stock  (synced stock)
```

Full API reference for frontend/backend developers: **[docs/API.md](docs/API.md)**

---

## 1. Prerequisites

| Need | Version / notes |
|---|---|
| Python | 3.12 (`python --version`) |
| Git | any recent version |
| Docker | optional, only for running it as a container |
| Credentials | see [Who to ask for what](#who-to-ask-for-what). Never put them in Git or chat. |

## 2. Setup

### Windows (PowerShell)

```bash
git clone https://github.com/interns-arch/flipkartand-amazon.git
```
```bash
cd flipkartand-amazon
```
```bash
python -m venv .venv
```
```bash
.venv\Scripts\pip install -r requirements.txt
```
```bash
copy .env.example .env
```

### macOS / Linux

```bash
git clone https://github.com/interns-arch/flipkartand-amazon.git && cd flipkartand-amazon
```
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && cp .env.example .env
```

On macOS/Linux, use `.venv/bin/` wherever this README says `.venv\Scripts\`.

## 3. Fill in `.env`

Open `.env` in an editor. At minimum, set:

1. **`ADMIN_TOKEN`**: any long random string. You can generate one with:
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
2. **Marketplace keys** for the channels you want, and set `AMAZON_ENABLED=true` and/or `FLIPKART_ENABLED=true` for them.
3. **`ORDER_API_URL`** (and `ORDER_API_TOKEN` if that API needs one). This is where new orders are sent. If you leave it empty, orders are fetched and kept, then retried every 5 minutes for up to 48 hours.

Leave the safety switches as they are while testing:

| Setting | Safe value | Meaning |
|---|---|---|
| `DRY_RUN` | `true` | Stock updates are only logged (`GET /stock/pushes`), never sent to Amazon/Flipkart |
| `PORTAL_WRITE_ENABLED` | `false` | Only for `PORTAL_MODE=rest`. Nothing is created on the Vagmine portal |

A blank line such as `AMAZON_SELLER_ID=` means "not set". That's fine for anything you don't have yet.

## 4. Run the tests

```bash
.venv\Scripts\python -m pytest -q
```

All tests use mocked APIs. They need no credentials and make no network calls.

## 5. Check the marketplace keys (read-only)

```bash
.venv\Scripts\python -m app.cli check-channels
```

This logs in to Amazon and Flipkart and counts recent orders. It changes nothing. Expected output:

```
Amazon  login: OK
Amazon  orders API: OK, 3 orders in last 7 days {'MFN': 3}
Flipkart login: OK
Flipkart orders API: OK, 2 open shipments in last 7 days
```

## 6. Start the service

```bash
.venv\Scripts\uvicorn app.main:app --port 8010
```

| URL | What |
|---|---|
| http://localhost:8010/tester | Test page: every order API with example requests/responses and a **Send** button |
| http://localhost:8010/docs | Auto-generated API docs |
| http://localhost:8010/health | Status, enabled channels, last sync results (no login needed) |

If port 8010 is taken, pick another (e.g. `--port 8020`).

On startup it begins checking for orders every 5 minutes (`POLL_INTERVAL_SECONDS`). To stop it from fetching while you develop, set `SCHEDULER_ENABLED=false`.

### Or run it with Docker

```bash
docker compose up -d --build
```

The container listens on `127.0.0.1:8010` only, keeps its database in `./data`, and restarts automatically. Stop it with `docker compose down`.

---

## How it works

### Modes (`PORTAL_MODE`)

| Mode | Where orders go | Where stock comes from |
|---|---|---|
| `api` **(default)** | `POST ORDER_API_URL` (see [docs/API.md](docs/API.md#outgoing-orders-what-order_api_url-receives)) | `POST /api/v1/stock` |
| `local` | Built-in test store in the local database | Built-in test store (`PUT /local-portal/stock`) |
| `rest` | Vagmine dealer portal: resolve customer → analyze → push order | Not available yet (no stock API) |

### SKU map

Stock can only be pushed for SKUs that are in the SKU map, which links our SKU to the Amazon SKU and the Flipkart SKU + FSN. Fill a CSV like [sku_map.example.csv](sku_map.example.csv) and load it:

```bash
.venv\Scripts\python -m app.cli import-skus sku_map.csv
```

In `api` mode, orders are forwarded even if a SKU isn't mapped. In that case the line has `sku: null` and still carries `channel_sku`.

### Common tasks

All `/api/v1`, `/sync` and `/sku-map` calls need the header `X-Admin-Token: <ADMIN_TOKEN>`.

| Task | Call |
|---|---|
| Send stock | `POST /api/v1/stock` with `{"items": [{"sku": "PSFA001B", "quantity": 5}]}` |
| Fetch orders now (don't wait 5 min) | `POST /sync/orders` |
| Re-sync all stock now | `POST /sync/stock` |
| See fetched orders and errors | `GET /orders/recent` |
| See stock pushes (incl. dry-run) | `GET /stock/pushes` |
| View / update the SKU map | `GET /sku-map`, `PUT /sku-map` |

Example with curl:
```bash
curl -X POST http://localhost:8010/api/v1/stock -H "X-Admin-Token: <ADMIN_TOKEN>" -H "Content-Type: application/json" -d "{\"items\":[{\"sku\":\"PSFA001B\",\"quantity\":5}]}"
```

### Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | Status and last sync results |
| POST | `/api/v1/stock` | admin | Receive synced stock and push it to the marketplaces |
| POST | `/api/v1/customers/resolve` | admin | Vagmine: find/create customer |
| POST | `/api/v1/orders/analyze` | admin | Vagmine: check parts before ordering (read-only) |
| POST | `/api/v1/orders` | admin | Vagmine: push order |
| POST | `/api/v1/orders/process` | admin | Vagmine: resolve → analyze → push in one call |
| GET | `/api/v1/orders`, `/api/v1/orders/{ref}` | admin | Vagmine orders sent by this service |
| GET | `/api/v1/orders/{ref}/track` | admin | Vagmine tracking (needs `PORTAL_TRACK_PATH`) |
| POST | `/sync/orders`, `/sync/stock` | admin | Run a sync now |
| GET/PUT | `/sku-map` | admin | SKU map |
| GET | `/orders/recent`, `/stock/pushes` | admin | Logs |
| POST | `/webhooks/portal/stock-changed` | `X-Webhook-Secret` | Push stock for the listed SKUs |
| PUT | `/local-portal/stock` | admin | `local` mode only: set test stock |

Request/response bodies and every error code: [docs/API.md](docs/API.md).

## Project layout

```
app/
  main.py            FastAPI app, scheduler, admin endpoints
  config.py          all settings (read from .env)
  sync.py            order fetching + stock pushing logic
  portal.py          where orders go / stock comes from (api, local, rest modes)
  channels/amazon.py   Amazon SP-API connector
  channels/flipkart.py Flipkart Seller API connector
  api_orders.py      /api/v1 endpoints (stock API + Vagmine order flow)
  order_flow.py, vagmine.py, portal_auth.py   Vagmine dealer-portal client
  db.py              SQLite storage (SKU map, order log, stock log)
  cli.py             import-skus, check-channels
  static/tester.html test page served at /tester
docs/API.md          API reference
tests/               pytest suite (mocked APIs)
```

## Go-live checklist

1. Fill `.env` with `DRY_RUN=true`, then run the tests and `check-channels`.
2. Load the SKU map.
3. Set `ORDER_API_URL`, start the service, call `POST /sync/orders`, and confirm orders arrive in our system and in `GET /orders/recent`.
4. Send stock to `POST /api/v1/stock` and check that `GET /stock/pushes` shows the expected quantities (dry-run).
5. Set `DRY_RUN=false`. Send stock for 1–2 test SKUs and verify them in Seller Central / Seller Hub.
6. Then enable everything.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Flipkart FAILED: HTTP 401 ... Self Access Application is not in Approved state` | Flipkart hasn't approved the app yet. Check Seller Hub → Developer Access. |
| Amazon `HTTP 403` | The app is missing a role in Developer Central (needs **Inventory and Order Tracking**, plus **Product Listing** for stock). |
| Order shows `ORDER_API_URL is not set yet` in `/orders/recent` | Set `ORDER_API_URL`. The order is resent automatically within 48 hours. |
| Order shows `no SKU mapping for ...` | Only in `local`/`rest` mode. Add the SKU to the SKU map. |
| Stock API returns SKUs under `unmapped` | They were saved but aren't in the SKU map, so they aren't pushed. |
| `AMAZON_SELLER_ID not set` / `FLIPKART_LOCATION_ID not set` in `/stock/pushes` | These are needed only for stock updates. Fill them in `.env`. |
| `address already in use` on start | Another app uses that port. Start with `--port 8020`. |
| `401 unauthorized` from the API | The `X-Admin-Token` header is missing or doesn't match `ADMIN_TOKEN` in `.env`. |

## Security

- **Never commit `.env`.** It's in `.gitignore`. Never paste keys or tokens into chat, tickets or screenshots.
- **Each developer uses their own `.env`.** Share credentials only through a password manager.
- **Exposed keys must be replaced.** If a key may have leaked, rotate it: Amazon Developer Central → LWA credentials; Flipkart Seller Hub → Developer Access.
- **Admin endpoints use `ADMIN_TOKEN`.** Don't expose the service publicly without HTTPS in front of it.

## Who to ask for what

| Item | Where it comes from | Ask |
|---|---|---|
| Flipkart App ID + Secret, approval status | Seller Hub → Manage Profile → Developer Access → Self Access | Manish |
| Flipkart location ID (`LOC…`) | Seller Hub → pickup/warehouse locations | Manish |
| Amazon LWA client ID + secret, refresh token | Solution Provider Portal → Developer Central | Manish |
| Amazon seller ID (Merchant Token) | Seller Central → Settings → Account Info | Manish |
| `ORDER_API_URL` / token | Our order API | Team lead |
| Vagmine login, branch/dealer IDs, tracking API | Vagmine dealer portal | Shubham |
| SKU map data | Exports from our system, Amazon and Flipkart | Team |
