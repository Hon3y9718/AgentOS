"""ASGI app entrypoint.

Role: wires lifespan, middleware, the domain-error exception handler, and the
liveness/readiness endpoints (API_CONTRACT §5.1). No business logic — that's
the boundary this file must never cross.
Called by: uvicorn (`uvicorn app.main:app`). Calls app.config, app.core.errors,
app.core.telemetry, app.db.session.
Gotcha: /health/ready's registry check is close to a formality, not a live
failure detector — app.core.llm.registry.ModelRegistry seeds every curated
catalog entry synchronously at construction (import time), so by the time
this endpoint can run at all, `len(registry)` is already at least
`len(catalog.CATALOG)`, independent of whether the live-refresh background
task below has run yet or ever succeeds. §5.1 explicitly requires this
endpoint to never call providers — it doesn't, then or now. See ADR-0002
decision 5 (updated 2026-07-21 for live model discovery).
See: docs/API_CONTRACT.md#51-health
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.v1.chat import router as chat_router
from app.api.v1.conversations import router as conversations_router
from app.api.v1.messages import router as messages_router
from app.api.v1.models import router as models_router
from app.config import settings
from app.core.errors import DomainError
from app.core.llm.registry import registry
from app.core.telemetry.logging import configure_logging
from app.core.telemetry.middleware import RequestIDMiddleware
from app.db.session import engine

logger = structlog.get_logger()

# WHY a module-level set, not a bare `asyncio.create_task(...)` call: a task
# with no strong reference held anywhere can be garbage-collected mid-flight
# (a real asyncio gotcha, not just a lint nitpick — see the stdlib docs for
# create_task). Holding it here until it finishes, then discarding it via
# the done-callback, is the standard fire-and-forget pattern.
_background_tasks: set[asyncio.Task[None]] = set()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Startup: configure logging, kick off a best-effort live model
    refresh. Shutdown: dispose the DB connection pool.

    Why disposing the pool matters: skipping it leaks open connections past
    process exit — harmless with --reload in dev, but in prod it delays
    container shutdown until postgres times the connections out itself.

    WHY `create_task`, not `await`ed directly: startup must not block on
    four providers' APIs (some/all of which may be slow, down, or simply
    not configured) — `registry.refresh_if_stale()` already treats a
    per-provider failure as non-fatal internally (see registry.py), and
    firing it as a background task means a slow provider delays nothing,
    not even its own refresh landing before the first request.
    """
    configure_logging()
    if settings.enable_live_model_refresh:
        task = asyncio.create_task(registry.refresh_if_stale())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    logger.info("startup")
    yield
    await engine.dispose()
    logger.info("shutdown")


app = FastAPI(title="AgentOS", lifespan=lifespan)
app.add_middleware(RequestIDMiddleware)
app.include_router(conversations_router, prefix="/api/v1")
app.include_router(messages_router, prefix="/api/v1")
app.include_router(chat_router, prefix="/api/v1")
app.include_router(models_router, prefix="/api/v1")


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    """The single place a DomainError becomes an HTTP response (API_CONTRACT §2).

    Every service-raised error passes through here — this is the only spot in
    the codebase allowed to know that DomainError maps to JSON over HTTP.
    """
    request_id = getattr(request.state, "request_id", None)
    return JSONResponse(status_code=exc.http_status, content={"error": exc.to_envelope(request_id)})


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe. No auth, no dependencies touched, always 200 if the process is up."""
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready() -> JSONResponse:
    """Readiness probe. Checks DB connectivity and registry load (§5.1);
    503 with per-check detail if either is down."""
    checks: dict[str, str] = {}
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        # WHY the response says "unreachable" but the log gets str(exc):
        # this endpoint is unauthenticated like /health, so a raw driver
        # error (which can include hostnames or auth failure detail) must
        # not reach the client — only the operator watching logs needs it.
        checks["database"] = "unreachable"
        logger.error("readiness_check_failed", check="database", error=str(exc))

    # WHY this can't meaningfully fail: `registry` seeds every catalog entry
    # synchronously at construction (import time) — a malformed catalog.yaml
    # would have crashed the process before it ever got this far. `len() ==
    # 0` is the only state left to catch (an empty but structurally valid
    # file). This check is unaffected by live model discovery: it never
    # reads live-refresh state, so §5.1's "does not call providers" holds
    # exactly as before.
    checks["registry"] = "ok" if len(registry) > 0 else "empty"

    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "unhealthy", "checks": checks},
    )
