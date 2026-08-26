from fastapi import FastAPI, APIRouter, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from app.api import auth, invoices, dashboard, vendors, conversation, audit, config, inbox, autopilot, delegations, users, requisitions, sourcing, purchase_orders, payments, bank_statements, watchlist, notifications, org_units, system, dashboard_export, inventory, hr, integrations
import logging
import uuid
from app.utils.datetime_helpers import utc_now
from app.core.config import settings
from app.core.logging_config import (
    configure_logging, new_request_id, request_id_var,
)

# Configure logging once, at import, so module-level `logger` calls actually
# surface. Without this the root logger has no handler under uvicorn and
# anything below WARNING is silently dropped — which is why parts of the code
# had fallen back to print().
#
# Readable lines in development, one JSON object per line in production, and
# every record carries the request id. See app/core/logging_config.py.
configure_logging(debug=settings.DEBUG)

logger = logging.getLogger(__name__)

# Debug toggle (default: production-safe)
DEBUG = settings.DEBUG

app = FastAPI(title="Sarmaya OS")

# CORS Configuration - MUST be added before routes. Origins come from settings
# (not a wildcard): "*" with allow_credentials=True is rejected by browsers and
# would defeat the credentialed (JWT) requests the frontend makes.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
    allow_methods=settings.CORS_ALLOW_METHODS,
    allow_headers=settings.CORS_ALLOW_HEADERS,
    max_age=3600,  # Cache preflight requests for 1 hour
)

# Versioned API router
api_v1 = APIRouter(prefix="/api/v1")

# include existing routers under /api/v1
api_v1.include_router(auth.router)
api_v1.include_router(invoices.router)
api_v1.include_router(dashboard.router)
api_v1.include_router(conversation.router)
api_v1.include_router(vendors.router)
api_v1.include_router(audit.router)
api_v1.include_router(config.router)
api_v1.include_router(inbox.router)
api_v1.include_router(autopilot.router)
api_v1.include_router(delegations.router)
api_v1.include_router(users.router)
api_v1.include_router(purchase_orders.router)
api_v1.include_router(requisitions.router)
api_v1.include_router(sourcing.router)
api_v1.include_router(payments.router)
api_v1.include_router(bank_statements.router)
api_v1.include_router(watchlist.router)
api_v1.include_router(notifications.router)
api_v1.include_router(org_units.router)
api_v1.include_router(system.router)
api_v1.include_router(dashboard_export.router)
api_v1.include_router(inventory.router)
api_v1.include_router(hr.router)
api_v1.include_router(integrations.router)


# Health check. Must be declared before the router is included: FastAPI copies
# a router's routes at include time, so anything registered afterwards is
# silently dropped — which is why this endpoint used to 404.
@api_v1.get("/ping")
async def ping():
    return {"pong": True}


# register versioned router once, after every route is attached
app.include_router(api_v1)


# root redirect to /api/v1
@app.get("/")
async def root():
    return RedirectResponse(url="/api/v1")


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Give every request an id, and put it on every log line it produces.

    Honours an inbound `X-Request-ID` so a caller that already has one — a
    gateway, the frontend, another service — keeps the same thread through our
    logs rather than starting a second one nobody can join up. Echoed on the
    response so the client can quote it.
    """
    request_id = request.headers.get("X-Request-ID") or new_request_id()
    token = request_id_var.set(request_id)
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        # Reset rather than leave it set: the context is reused across
        # requests, and a leaked id would label the next request's logs with
        # the previous request's identity.
        request_id_var.reset(token)


# -----------------------
# Global 500 Exception Handler
# -----------------------
@app.exception_handler(Exception)
async def internal_exception_handler(request: Request, exc: Exception):
    """
    Catch-all handler for unhandled exceptions to return a consistent JSON payload
    without leaking internals. Full traceback is logged server-side with a correlation id.
    """
    # The id the middleware already put on every log line this request
    # produced. Previously this handler minted its own, which meant the id in
    # the client's error payload matched exactly one log entry — this one —
    # and nothing that led up to it. The fallback covers a failure early
    # enough that the middleware never ran.
    correlation_id = request_id_var.get() or str(uuid.uuid4())

    # Timestamp in UTC ISO format
    ts = utc_now().isoformat()

    # Log full exception + request info server-side (safe)
    try:
        logger.exception(
            "Unhandled exception occurred: correlation_id=%s method=%s path=%s client=%s exc=%s",
            correlation_id,
            request.method,
            request.url.path,
            request.client.host if request.client else None,
            repr(exc),
        )
    except Exception:
        # ensure logging errors don't break the handler
        logger.error("Failed to log exception details for correlation_id=%s", correlation_id)

    # Minimal, non-sensitive payload for clients
    payload = {
        "success": False,
        "error": {
            "code": "internal_server_error",
            "message": "An internal server error occurred. Please contact support.",
            "timestamp": ts,
            "correlation_id": correlation_id,
        },
    }

    # Optionally include non-sensitive debug info if explicitly enabled (NOT recommended in production)
    if DEBUG:
        # Keep this small and intentionally non-sensitive; do NOT include full trace in prod.
        payload["error"]["debug"] = {
            "exception": type(exc).__name__,
            "message": str(exc)[:512],  # truncate to avoid huge leaks
        }

    return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content=payload)
