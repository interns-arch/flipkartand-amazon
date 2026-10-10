"""Order flow on the dealer portal: resolve customer -> analyze products -> push order -> track.

The portal's reply formats are not documented yet, so ids and availability are read
tolerantly (see find_value / analyze_problems). Every portal reply is stored in full
on the order record and shown on the tester page, so these rules can be tightened
once real replies are seen.
"""

import asyncio
import logging

import httpx

from app.config import Settings
from app.db import Database, now_iso
from app.models import ChannelOrder, CustomerIn, OrderIn, OrderItemIn, ProcessOrderIn
from app.portal_auth import PortalAuth
from app.vagmine import FlowError, VagmineClient

log = logging.getLogger(__name__)

CUSTOMER_ID_KEYS = ("customer_account_id", "customerAccountId", "account_id")
USER_ID_KEYS = ("order_for_user_id", "orderForUserId", "user_id")
ORDER_ID_KEYS = ("order_id", "orderId", "order_no", "order_number", "id")
NESTED_KEYS = ("data", "result", "customer", "account", "order")

# analyze: a False value for any of these flags means the order cannot be pushed
ORDER_FLAGS = ("success", "ok", "can_push", "can_place_order", "valid", "is_valid", "all_available")
ITEM_LIST_KEYS = ("items", "lines", "results", "parts")
ITEM_FLAGS = ("available", "is_available", "in_stock", "valid", "is_valid")
BAD_ITEM_STATUSES = {"UNAVAILABLE", "NOT_AVAILABLE", "OUT_OF_STOCK", "INSUFFICIENT_STOCK", "NOT_FOUND", "INVALID"}


def _containers(body) -> list[dict]:
    """The reply itself plus common envelopes like {"data": {...}} or {"data": {"customer": {...}}}."""
    found, queue = [], [body]
    while queue:
        current = queue.pop(0)
        if isinstance(current, dict):
            found.append(current)
            queue.extend(current[k] for k in NESTED_KEYS if isinstance(current.get(k), dict))
    return found


def find_value(body, keys: tuple[str, ...]):
    for container in _containers(body):
        for key in keys:
            if container.get(key) not in (None, ""):
                return container[key]
    return None


def analyze_problems(body) -> list[str]:
    problems = []
    for container in _containers(body):
        problems += [f"{flag} is false" for flag in ORDER_FLAGS if container.get(flag) is False]
        for list_key in ITEM_LIST_KEYS:
            items = container.get(list_key)
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                status = str(item.get("status") or "").upper()
                if any(item.get(flag) is False for flag in ITEM_FLAGS) or status in BAD_ITEM_STATUSES:
                    part = item.get("part_no") or item.get("partNo") or "?"
                    problems.append(f"{part}: {item.get('status') or item.get('message') or 'not available'}")
    return problems


class OrderFlow:
    def __init__(self, settings: Settings, db: Database, client: VagmineClient):
        self.settings = settings
        self.db = db
        self.client = client
        self._lock = asyncio.Lock()  # one push at a time, so a reference can never be pushed twice

    # --- payload builders --------------------------------------------------

    def _require_writes(self, step: str) -> None:
        if not self.settings.portal_write_enabled:
            raise FlowError(
                403,
                "portal_writes_disabled",
                "creating customers/orders on the dealer portal is disabled (set PORTAL_WRITE_ENABLED=true)",
                step,
            )

    @staticmethod
    def _required(value, field: str, setting: str, step: str):
        if value is None:
            raise FlowError(422, "missing_field", f"{field} is required (in the request or {setting} in .env)", step)
        return value

    def resolve_payload(self, customer: CustomerIn) -> dict:
        home = customer.home_branch_dealer_id or self.settings.portal_home_branch_dealer_id
        return {
            "shopify_customer_id": customer.external_customer_id,
            "name": customer.name,
            "phone": customer.phone,
            "email": customer.email,
            "address": customer.address,
            "gst_no": customer.gst_no,
            "home_branch_dealer_id": self._required(
                home, "home_branch_dealer_id", "PORTAL_HOME_BRANCH_DEALER_ID", "resolve"
            ),
        }

    def order_payload(self, order: OrderIn, step: str) -> dict:
        return {
            "order_reference_no": order.order_reference_no,
            "source_branch_dealer_id": self._required(
                order.source_branch_dealer_id or self.settings.portal_source_branch_dealer_id,
                "source_branch_dealer_id", "PORTAL_SOURCE_BRANCH_DEALER_ID", step,
            ),
            "items": [item.model_dump() for item in order.items],
            "order_for_user_id": self._required(
                order.order_for_user_id or self.settings.portal_order_for_user_id,
                "order_for_user_id", "PORTAL_ORDER_FOR_USER_ID", step,
            ),
            "customer_account_id": order.customer_account_id,
        }

    # --- single steps (used directly by the API and by process) -------------

    async def resolve_customer(self, customer: CustomerIn) -> dict:
        self._require_writes("resolve")
        status, body = await self.client.resolve_customer(self.resolve_payload(customer))
        customer_account_id = find_value(body, CUSTOMER_ID_KEYS)
        if customer_account_id is None:
            raise FlowError(502, "unexpected_portal_response",
                            "portal accepted the customer but its reply has no customer_account_id",
                            "resolve", status, body)
        return {
            "customer_account_id": customer_account_id,
            "order_for_user_id": find_value(body, USER_ID_KEYS),
            "portal_status": status,
            "portal_response": body,
        }

    async def analyze(self, order: OrderIn) -> dict:
        status, body = await self.client.analyze(self.order_payload(order, "analyze"))
        problems = analyze_problems(body)
        return {"can_push": not problems, "problems": problems, "portal_status": status, "portal_response": body}

    async def push_order(self, order: OrderIn) -> dict:
        self._require_writes("push_order")
        payload = self.order_payload(order, "push_order")
        async with self._lock:
            record = self._start_record(order.order_reference_no, {"order": payload})
            try:
                status, body = await self._step(record, "push_order", payload, self.client.push_order)
            except FlowError as exc:
                self._fail(record, exc)
                raise
            return self._finish(record, order.customer_account_id, body)

    async def track(self, order_reference_no: str) -> dict:
        status, body = await self.client.track(order_reference_no)
        return {"order_reference_no": order_reference_no, "portal_status": status, "portal_response": body}

    # --- full flow ----------------------------------------------------------

    async def process(self, req: ProcessOrderIn) -> dict:
        self._require_writes("resolve")
        async with self._lock:
            record = self._start_record(req.order_reference_no, req.model_dump())
            try:
                resolve_payload = self.resolve_payload(req.customer)
                _, resolved = await self._step(record, "resolve", resolve_payload, self.client.resolve_customer)
                customer_account_id = find_value(resolved, CUSTOMER_ID_KEYS)
                if customer_account_id is None:
                    raise FlowError(502, "unexpected_portal_response",
                                    "portal accepted the customer but its reply has no customer_account_id",
                                    "resolve", record["steps"]["resolve"]["portal_status"], resolved)
                record["customer_account_id"] = customer_account_id

                order = OrderIn(
                    order_reference_no=req.order_reference_no,
                    items=req.items,
                    customer_account_id=customer_account_id,
                    order_for_user_id=req.order_for_user_id or find_value(resolved, USER_ID_KEYS),
                    source_branch_dealer_id=req.source_branch_dealer_id,
                )
                order_payload = self.order_payload(order, "analyze")
                analyze_status, analysis = await self._step(record, "analyze", order_payload, self.client.analyze)
                problems = analyze_problems(analysis)
                if problems:
                    raise FlowError(422, "items_unavailable", "analyze reports the order cannot be placed",
                                    "analyze", analyze_status, analysis, {"problems": problems})

                _, pushed = await self._step(record, "push_order", order_payload, self.client.push_order)
            except FlowError as exc:
                self._fail(record, exc)
                raise
            return self._finish(record, customer_account_id, pushed)

    # --- record keeping -----------------------------------------------------

    def _start_record(self, ref: str, request: dict) -> dict:
        existing = self.db.get_portal_order(ref)
        if existing and existing["status"] == "pushed":
            raise FlowError(409, "duplicate_order", f"order {ref} was already pushed to the dealer portal",
                            details={"portal_order_id": existing["portal_order_id"],
                                     "pushed_at": existing["updated_at"]})
        record = {
            "order_reference_no": ref,
            "status": "processing",
            "customer_account_id": None,
            "portal_order_id": None,
            "request": request,
            "steps": {},
            "error": None,
            "created_at": existing["created_at"] if existing else now_iso(),
        }
        self.db.save_portal_order(record)
        return record

    async def _step(self, record: dict, name: str, payload: dict, call) -> tuple[int, object]:
        status, body = await call(payload)
        record["steps"][name] = {"ok": True, "at": now_iso(), "request": payload,
                                 "portal_status": status, "portal_response": body}
        self.db.save_portal_order(record)
        return status, body

    def _fail(self, record: dict, exc: FlowError) -> None:
        step = exc.step or "process"
        record["status"] = f"{step}_failed"
        record["error"] = exc.to_dict()["error"]
        if not record["steps"].get(step, {}).get("ok"):
            record["steps"][step] = {"ok": False, "at": now_iso(), "portal_status": exc.portal_status,
                                     "portal_response": exc.portal_response, "error": exc.message}
        self.db.save_portal_order(record)
        log.warning("Order %s failed at %s: %s", record["order_reference_no"], step, exc.message)

    def _finish(self, record: dict, customer_account_id, push_body) -> dict:
        record["status"] = "pushed"
        record["customer_account_id"] = customer_account_id
        portal_order_id = find_value(push_body, ORDER_ID_KEYS)
        record["portal_order_id"] = str(portal_order_id) if portal_order_id is not None else None
        record["error"] = None
        self.db.save_portal_order(record)
        log.info("Order %s pushed to dealer portal (portal id %s)", record["order_reference_no"], portal_order_id)
        return self.db.get_portal_order(record["order_reference_no"])


# --- Marketplace orders -> flow input -------------------------------------------


def _join(*parts) -> str:
    return ", ".join(str(p).strip() for p in parts if p and str(p).strip())


def process_request_from_channel_order(order: ChannelOrder) -> ProcessOrderIn:
    """Builds the portal flow input for an Amazon/Flipkart order.

    Buyers with a phone number get their own customer account (keyed channel:phone).
    Without one (Amazon hides buyer details unless the restricted PII role is approved)
    the order goes to one shared customer per channel.
    """
    addr = order.shipping_address or {}
    channel = order.channel.capitalize()
    phone = addr.get("contactNumber") or addr.get("Phone")
    name = " ".join(p for p in (addr.get("firstName"), addr.get("lastName")) if p) or addr.get("Name")
    address = _join(
        addr.get("addressLine1") or addr.get("AddressLine1"),
        addr.get("addressLine2") or addr.get("AddressLine2"),
        addr.get("landmark"),
        addr.get("city") or addr.get("City"),
        addr.get("state") or addr.get("StateOrRegion"),
        addr.get("pincode") or addr.get("PostalCode"),
    )
    if phone:
        customer = CustomerIn(external_customer_id=f"{order.channel}:{phone}", name=name or f"{channel} Customer",
                              phone=phone, address=address or None)
    else:
        customer = CustomerIn(external_customer_id=f"{order.channel}:marketplace",
                              name=f"{channel} Marketplace Customer")
    return ProcessOrderIn(
        order_reference_no=f"{order.channel.upper()}-{order.channel_order_id}",
        customer=customer,
        items=[OrderItemIn(part_no=line.sku, quantity=line.quantity) for line in order.lines],
    )


def build_order_flow(settings: Settings, db: Database, http: httpx.AsyncClient) -> OrderFlow | None:
    """None when portal login details are missing from .env."""
    if not (settings.portal_base_url and settings.portal_username and settings.portal_password):
        return None
    return OrderFlow(settings, db, VagmineClient(settings, PortalAuth(settings, http)))
