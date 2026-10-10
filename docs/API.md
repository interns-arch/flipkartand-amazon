# Order API: frontend reference

Base URL: wherever this service runs, e.g. `http://localhost:8000`.
Interactive version with live **Send** buttons: `GET /tester`. Auto-generated schema: `GET /docs`.

## Basics

- **Auth:** every `/api/v1` call needs the header `X-Admin-Token: <ADMIN_TOKEN from .env>`.
- **Body:** JSON with `Content-Type: application/json`.
- **Errors:** every error reply has the same shape. `step`, `details`, `portal_status` and `portal_response` appear only when relevant.

```json
{
  "error": {
    "code": "portal_rejected",
    "message": "dealer portal rejected the request",
    "step": "push_order",
    "details": {},
    "portal_status": 400,
    "portal_response": { "detail": "…exact dealer-portal reply…" }
  }
}
```

The frontend should branch on **`error.code`**, not on the message text.

## Flow

```
1 POST /api/v1/customers/resolve   -> customer_account_id
2 POST /api/v1/orders/analyze      -> can_push true/false   (read-only)
3 POST /api/v1/orders              -> order placed          (once per order_reference_no)
4 GET  /api/v1/orders/{ref}/track  -> portal status

or 1->3 in one call: POST /api/v1/orders/process
```

---

## 1. Resolve customer: `POST /api/v1/customers/resolve`

Finds the customer on the dealer portal, or creates them.

**Request**
```json
{
  "external_customer_id": "amazon:9000000000",
  "name": "Test Customer",
  "phone": "9000000000",
  "email": null,
  "address": "12 Test Street, Namakkal, Tamil Nadu, 637214",
  "gst_no": null,
  "home_branch_dealer_id": 23
}
```
| Field | Required | Notes |
|---|---|---|
| external_customer_id | yes | Stable id from the sales channel. Sent to the portal as `shopify_customer_id`. |
| name | yes | |
| phone, email, address, gst_no | no | |
| home_branch_dealer_id | no | Defaults to `PORTAL_HOME_BRANCH_DEALER_ID` |

**200 OK**
```json
{ "customer_account_id": 2564, "order_for_user_id": 4, "portal_status": 200, "portal_response": { } }
```

## 2. Analyze products: `POST /api/v1/orders/analyze`

Read-only check before placing the order. A result of `can_push: false` still returns **200**.

**Request**
```json
{
  "order_reference_no": "TEST-0001",
  "items": [{ "part_no": "PSFA001B", "quantity": 1 }],
  "customer_account_id": 2564,
  "order_for_user_id": 4,
  "source_branch_dealer_id": 23
}
```
| Field | Required | Notes |
|---|---|---|
| order_reference_no | yes | 1–100 characters, unique per order |
| items[] | yes, at least 1 | `part_no` (text), `quantity` (> 0) |
| customer_account_id | yes | From step 1 |
| order_for_user_id | no | Defaults to `PORTAL_ORDER_FOR_USER_ID` |
| source_branch_dealer_id | no | Defaults to `PORTAL_SOURCE_BRANCH_DEALER_ID` |

**200 OK**
```json
{ "can_push": true,  "problems": [], "portal_status": 200, "portal_response": { } }
{ "can_push": false, "problems": ["PSFA001B: OUT_OF_STOCK"], "portal_status": 200, "portal_response": { } }
```

## 3. Push order: `POST /api/v1/orders`

The request body is the same as Analyze. Each `order_reference_no` can only be pushed once.

**201 Created**: the saved order record (see [Order record](#order-record)).

## 1→3. Process order: `POST /api/v1/orders/process`

Runs resolve → analyze → push and stops at the first step that fails. Amazon and Flipkart orders use this. Calling it again for a failed order retries that order.

**Request**
```json
{
  "order_reference_no": "TEST-0001",
  "customer": { "external_customer_id": "amazon:9000000000", "name": "Test Customer", "phone": "9000000000" },
  "items": [{ "part_no": "PSFA001B", "quantity": 1 }],
  "order_for_user_id": 4,
  "source_branch_dealer_id": 23
}
```
`customer` takes the same fields as Resolve. `order_for_user_id` and `source_branch_dealer_id` are optional.

**201 Created**: the order record with all three steps.

## List orders: `GET /api/v1/orders?status=&limit=50`

Returns orders newest first. `status` can be `processing`, `pushed`, `resolve_failed`, `analyze_failed` or `push_order_failed`. `limit` is at most 500.
**200 OK**: an array of order records.

## Get order: `GET /api/v1/orders/{order_reference_no}`

**200 OK**: the order record. **404** `order_not_found`.

## 4. Track order: `GET /api/v1/orders/{order_reference_no}/track`

**200 OK**
```json
{ "order_reference_no": "TEST-0001", "portal_status": 200, "portal_response": { } }
```
This returns **501** `tracking_not_configured` until `PORTAL_TRACK_PATH` is set. That is waiting on the tracking API docs from Shubham.

---

## Order record

```json
{
  "order_reference_no": "TEST-0001",
  "status": "pushed",
  "customer_account_id": 2564,
  "portal_order_id": "98765",
  "request": { },
  "steps": {
    "resolve":    { "ok": true, "at": "2026-10-09T10:00:00+00:00", "request": { }, "portal_status": 200, "portal_response": { } },
    "analyze":    { "ok": true, "at": "…", "request": { }, "portal_status": 200, "portal_response": { } },
    "push_order": { "ok": true, "at": "…", "request": { }, "portal_status": 201, "portal_response": { } }
  },
  "error": null,
  "created_at": "2026-10-09T10:00:00+00:00",
  "updated_at": "2026-10-09T10:00:02+00:00"
}
```
For a failed order:
- `status` is `<step>_failed`.
- `error` holds the error object.
- The failed step has `"ok": false`, plus the portal's status code and reply.

## Status codes

| HTTP | error.code | Endpoints | When |
|---|---|---|---|
| 200 / 201 | – | all | Success (201 for push and process) |
| 401 | `unauthorized` | all `/api/v1` | Missing or wrong `X-Admin-Token` |
| 403 | `portal_writes_disabled` | resolve, push, process | Safety switch `PORTAL_WRITE_ENABLED=false` |
| 404 | `order_not_found` | get | Unknown reference |
| 409 | `duplicate_order` | push, process | Reference already pushed (`details.portal_order_id`) |
| 409 | `portal_conflict` | push, process | The portal itself reports a conflict |
| 422 | `validation_error` | all with a body | Missing or wrong field (`details.errors`) |
| 422 | `missing_field` | resolve, analyze, push, process | A dealer/user id is in neither the request nor `.env` |
| 422 | `items_unavailable` | process | Analyze says the order can't be placed (`details.problems`). Nothing was pushed. |
| 422 | `portal_rejected` | portal calls | The portal answered 4xx; its reply is in `portal_response` |
| 501 | `tracking_not_configured` | track | Tracking API not set up yet |
| 502 | `portal_auth_failed` | portal calls | Portal login failed, or the account lacks a permission |
| 502 | `portal_error` | portal calls | The portal answered 5xx |
| 502 | `portal_unreachable` | portal calls | Network, DNS or TLS failure |
| 502 | `unexpected_portal_response` | resolve, process | Portal said OK but sent no `customer_account_id` |
| 503 | `portal_not_configured` | portal calls | Portal URL, username or password missing from `.env` |
| 504 | `portal_timeout` | portal calls | The portal took longer than 30 s |

---

## Stock API: `POST /api/v1/stock`

Send your synced stock here. The stock for every SKU that's in the SKU map is then pushed to Amazon and Flipkart in the background. Needs `PORTAL_MODE=api` and the `X-Admin-Token` header.

**Request**
```json
{ "items": [ { "sku": "PSFA001B", "quantity": 5 }, { "sku": "NEW-PART", "quantity": 3 } ] }
```
| Field | Rules |
|---|---|
| items | 1–5000 entries |
| items[].sku | Your SKU / part number (`portal_sku` in the SKU map) |
| items[].quantity | Whole number, 0 or more. If a SKU appears twice, the last value counts. |

**202 Accepted**
```json
{ "accepted": 2, "pushing": ["PSFA001B"], "unmapped": ["NEW-PART"], "dry_run": true }
```
- `pushing`: SKUs that will be updated on the marketplaces.
- `unmapped`: SKUs that were saved but aren't in the SKU map yet, so they aren't pushed.
- `dry_run: true`: the pushes are only logged, not sent (`DRY_RUN=true`).

The result of each push appears in `GET /stock/pushes`.

| HTTP | error.code | When |
|---|---|---|
| 401 | `unauthorized` | Missing or wrong `X-Admin-Token` |
| 409 | `stock_api_disabled` | `PORTAL_MODE` is not `api` |
| 422 | `validation_error` | Empty list, missing SKU, negative quantity, more than 5000 items |

When an Amazon or Flipkart order is delivered, the sold units are subtracted from the stored stock until your next stock update replaces it.

## Outgoing orders: what `ORDER_API_URL` receives

With `PORTAL_MODE=api`, the service sends every new seller-fulfilled Amazon/Flipkart order to `ORDER_API_URL`:

```
POST <ORDER_API_URL>
Content-Type: application/json
Idempotency-Key: amazon-403-1234567-1234567
Authorization: Bearer <ORDER_API_TOKEN>        (only if set)
```
```json
{
  "channel": "amazon",
  "channel_order_id": "403-1234567-1234567",
  "status": "Unshipped",
  "ordered_at": "2026-10-09T10:00:00Z",
  "seller_fulfilled": true,
  "currency": "INR",
  "total": 999.0,
  "shipping_address": { "City": "Namakkal", "StateOrRegion": "Tamil Nadu", "PostalCode": "637214" },
  "lines": [
    { "channel_sku": "AMZ-PSFA001B", "sku": "PSFA001B", "quantity": 1, "line_total": 999.0, "title": "…", "channel_line_id": "123" }
  ]
}
```
- `sku` is your SKU from the SKU map, or `null` if the marketplace SKU isn't mapped. `channel_sku` is always the marketplace's own SKU.
- **Reply:** any 2xx means received. An `{"id": "..."}` in the reply body is saved as your order ID. 409 means "already received" and also counts as success.
- **Other replies (or no `ORDER_API_URL` yet):** the order is retried every 5 minutes for up to 48 hours. Use `Idempotency-Key` to ignore repeats.
