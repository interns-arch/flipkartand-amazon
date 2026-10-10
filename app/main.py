import asyncio
import logging
import platform
import secrets
import time
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone

from pathlib import Path

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from app.api_orders import flow_error_handler, require_admin
from app.api_orders import router as orders_router
from app.channels import build_channels
from app.config import Settings, get_settings
from app.db import Database
from app.models import SkuMapping
from app.order_flow import build_order_flow
from app.portal import build_portal
from app.sync import SyncService
from app.vagmine import FlowError

APP_NAME = "cartrends-marketplace-sync"
APP_VERSION = "0.3.0"
TESTER_PAGE = Path(__file__).parent / "static" / "tester.html"
START_TIME = time.monotonic()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(APP_NAME)


async def run_scheduler(service: SyncService, settings: Settings) -> None:
    last_reconcile = 0.0
    while True:
        try:
            await service.pull_orders()
            if time.monotonic() - last_reconcile >= settings.reconcile_interval_seconds:
                await service.push_stock()
                last_reconcile = time.monotonic()
        except Exception:
            log.exception("Scheduled sync failed")
        await asyncio.sleep(settings.poll_interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    db = Database(settings.database_path)
    db.init()
    http = httpx.AsyncClient(timeout=30)
    order_flow = build_order_flow(settings, db, http)
    service = SyncService(settings, db, build_portal(settings, db, order_flow, http), build_channels(settings, http))
    app.state.settings, app.state.db, app.state.service = settings, db, service
    app.state.order_flow = order_flow

    task = asyncio.create_task(run_scheduler(service, settings)) if settings.scheduler_enabled else None
    log.info("Started: channels=%s portal=%s dry_run=%s", list(service.channels), settings.portal_mode, settings.dry_run)
    yield
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    await http.aclose()


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)
app.include_router(orders_router)
app.add_exception_handler(FlowError, flow_error_handler)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "validation_error", "message": "request is invalid",
                           "details": {"errors": jsonable_encoder(exc.errors())}}},
    )


admin = [Depends(require_admin)]


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/tester")


@app.get("/tester", response_class=HTMLResponse, include_in_schema=False)
def tester_page() -> str:
    """Test page for the order API (request/response examples, success and failure codes)."""
    return TESTER_PAGE.read_text(encoding="utf-8")


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    timestamp: datetime
    uptime_seconds: float
    python_version: str
    channels: list[str]
    portal_mode: str
    portal_configured: bool
    portal_write_enabled: bool
    tracking_configured: bool
    dry_run: bool
    last_order_pull: dict | None
    last_stock_push: dict | None


class SkuList(BaseModel):
    skus: list[str] | None = None


@app.get("/health", response_model=HealthResponse, tags=["health"])
def health(request: Request) -> HealthResponse:
    """Liveness check: returns 200 if the service is running, plus last sync results."""
    service: SyncService = request.app.state.service
    return HealthResponse(
        status="ok",
        service=APP_NAME,
        version=APP_VERSION,
        timestamp=datetime.now(timezone.utc),
        uptime_seconds=round(time.monotonic() - START_TIME, 2),
        python_version=platform.python_version(),
        channels=list(service.channels),
        portal_mode=service.settings.portal_mode,
        portal_configured=request.app.state.order_flow is not None,
        portal_write_enabled=service.settings.portal_write_enabled,
        tracking_configured=bool(service.settings.portal_track_path),
        dry_run=service.settings.dry_run,
        last_order_pull=service.status["last_order_pull"],
        last_stock_push=service.status["last_stock_push"],
    )


@app.post("/sync/orders", dependencies=admin, tags=["sync"])
async def sync_orders(request: Request) -> dict:
    """Pull new orders from all marketplaces into the dealer portal right now."""
    return await request.app.state.service.pull_orders()


@app.post("/sync/stock", dependencies=admin, tags=["sync"])
async def sync_stock(request: Request, body: SkuList | None = None) -> dict:
    """Push portal stock to all marketplaces (all mapped SKUs if none given)."""
    return await request.app.state.service.push_stock(body.skus if body else None)


@app.post("/webhooks/portal/stock-changed", tags=["webhooks"])
async def portal_stock_changed(
    body: SkuList,
    request: Request,
    background: BackgroundTasks,
    x_webhook_secret: str = Header(default=""),
) -> dict:
    """The dealer portal calls this whenever stock changes, so marketplaces update within seconds."""
    expected = request.app.state.settings.portal_webhook_secret
    if not expected or not secrets.compare_digest(x_webhook_secret, expected):
        raise HTTPException(status_code=401, detail="invalid webhook secret")
    if not body.skus:
        raise HTTPException(status_code=422, detail="skus is required")
    background.add_task(request.app.state.service.push_stock, body.skus)
    return {"queued": len(body.skus)}


@app.get("/sku-map", dependencies=admin, tags=["sku map"])
def list_sku_map(request: Request) -> list[SkuMapping]:
    return request.app.state.db.list_mappings(active_only=False)


@app.put("/sku-map", dependencies=admin, tags=["sku map"])
def upsert_sku_map(mappings: list[SkuMapping], request: Request) -> dict:
    return {"saved": request.app.state.db.upsert_mappings(mappings)}


@app.get("/orders/recent", dependencies=admin, tags=["reports"])
def recent_orders(request: Request, limit: int = 50) -> list[dict]:
    return request.app.state.db.recent_orders(limit)


@app.get("/stock/pushes", dependencies=admin, tags=["reports"])
def recent_pushes(request: Request, limit: int = 100) -> list[dict]:
    return request.app.state.db.recent_pushes(limit)


@app.put("/local-portal/stock", dependencies=admin, tags=["local portal"])
def set_local_stock(stock: dict[str, int], request: Request) -> dict:
    """Test-only: set stock in the built-in portal (PORTAL_MODE=local)."""
    if request.app.state.settings.portal_mode != "local":
        raise HTTPException(status_code=400, detail="only available when PORTAL_MODE=local")
    request.app.state.db.local_set_stock(stock)
    return {"saved": len(stock)}
