from fastapi import FastAPI, APIRouter, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from app.api import auth, invoices, dashboard, vendors, conversation, audit, config, inbox, autopilot, delegations
import logging
import uuid
from app.utils.datetime_helpers import utc_now
from app.core.config import settings

# configure logger (uses root logger config; adjust as needed)
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

# register versioned router once
app.include_router(api_v1)

# root redirect to /api/v1
@app.get("/")
async def root():
    return RedirectResponse(url="/api/v1")

# ping endpoint under api v1
@api_v1.get("/ping")
async def ping():
    return {"pong": True}


# -----------------------
# Global 500 Exception Handler
# -----------------------
@app.exception_handler(Exception)
async def internal_exception_handler(request: Request, exc: Exception):
    """
    Catch-all handler for unhandled exceptions to return a consistent JSON payload
    without leaking internals. Full traceback is logged server-side with a correlation id.
    """
    # Prefer any incoming request id header so client/server can correlate logs
    incoming_req_id = request.headers.get("X-Request-ID")
    correlation_id = incoming_req_id or str(uuid.uuid4())

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
