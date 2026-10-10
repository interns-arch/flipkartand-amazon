"""Order API for the frontend (/api/v1): wraps the dealer-portal order flow.

Every error reply has the shape:
    {"error": {"code": "...", "message": "...", "step": "...", "portal_status": 400, "portal_response": {...}}}
"""

import secrets

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request
from fastapi.responses import JSONResponse

from app.models import CustomerIn, OrderIn, ProcessOrderIn, StockIn
from app.order_flow import OrderFlow
from app.vagmine import FlowError


def require_admin(request: Request, x_admin_token: str = Header(default="")) -> None:
    expected = request.app.state.settings.admin_token
    if not expected or not secrets.compare_digest(x_admin_token, expected):
        raise FlowError(401, "unauthorized", "missing or invalid X-Admin-Token header")


def get_flow(request: Request) -> OrderFlow:
    flow = request.app.state.order_flow
    if flow is None:
        raise FlowError(503, "portal_not_configured",
                        "dealer portal login is not configured (PORTAL_BASE_URL, PORTAL_USERNAME, PORTAL_PASSWORD)")
    return flow


router = APIRouter(prefix="/api/v1", tags=["dealer portal orders"], dependencies=[Depends(require_admin)])


@router.post("/customers/resolve")
async def resolve_customer(customer: CustomerIn, flow: OrderFlow = Depends(get_flow)) -> dict:
    """Step 1: find the customer on the dealer portal, or create them. Returns customer_account_id."""
    return await flow.resolve_customer(customer)


@router.post("/orders/analyze")
async def analyze_order(order: OrderIn, flow: OrderFlow = Depends(get_flow)) -> dict:
    """Step 2: check the products before placing the order. Read-only; can_push says whether it can go ahead."""
    return await flow.analyze(order)


@router.post("/orders", status_code=201)
async def push_order(order: OrderIn, flow: OrderFlow = Depends(get_flow)) -> dict:
    """Step 3: place the order on the dealer portal. A reference that was already pushed returns 409."""
    return await flow.push_order(order)


@router.post("/orders/process", status_code=201)
async def process_order(req: ProcessOrderIn, flow: OrderFlow = Depends(get_flow)) -> dict:
    """Steps 1-3 in one call: resolve customer -> analyze -> push order."""
    return await flow.process(req)


@router.get("/orders")
def list_orders(request: Request, status: str | None = None, limit: int = 50) -> list[dict]:
    """Orders sent through this service, newest first. Filter with ?status=pushed|..._failed."""
    return request.app.state.db.list_portal_orders(limit=min(limit, 500), status=status)


@router.get("/orders/{order_reference_no}")
def get_order(order_reference_no: str, request: Request) -> dict:
    """One order with every step's request and portal reply."""
    record = request.app.state.db.get_portal_order(order_reference_no)
    if record is None:
        raise FlowError(404, "order_not_found", f"no order with reference {order_reference_no}")
    return record


@router.get("/orders/{order_reference_no}/track")
async def track_order(order_reference_no: str, flow: OrderFlow = Depends(get_flow)) -> dict:
    """Step 4: current status from the dealer portal's tracking API."""
    return await flow.track(order_reference_no)


@router.post("/stock", status_code=202, tags=["stock"])
async def receive_stock(body: StockIn, request: Request, background: BackgroundTasks) -> dict:
    """Send synced stock; mapped SKUs are then pushed to Amazon/Flipkart in the background.

    Results of each push appear in GET /stock/pushes. Needs PORTAL_MODE=api.
    """
    state = request.app.state
    if state.settings.portal_mode != "api":
        raise FlowError(409, "stock_api_disabled", "the stock API needs PORTAL_MODE=api")
    stock = {item.sku.strip(): item.quantity for item in body.items}  # a repeated SKU: last value wins
    state.db.set_synced_stock(stock)
    mapped = sorted(m.portal_sku for m in state.db.list_mappings(stock))
    if mapped:
        background.add_task(state.service.push_stock, mapped)
    return {
        "accepted": len(stock),
        "pushing": mapped,
        "unmapped": sorted(set(stock) - set(mapped)),
        "dry_run": state.settings.dry_run,
    }


async def flow_error_handler(request: Request, exc: FlowError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict())
