"""ASGI app entrypoint.

Role: wires lifespan, middleware, the domain-error exception handler, and the
liveness/readiness endpoints (API_CONTRACT §5.1). No business logic — that's
the boundary this file must never cross.
Called by: uvicorn (`uvicorn app.main:app`). Calls app.config, app.core.errors,
app.core.telemetry, app.db.session.
Gotcha: /health/ready's registry check is close to a formality, not a live
failure detector — app.core.llm.registry loads and validates registry.yaml
at import time (same crash-at-boot pattern as Settings()), so by the time
this endpoint can run at all, the registry has already loaded successfully.
See ADR-0002 decision 5.
See: docs/API_CONTRACT.md#51-health
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.v1.chat import router as chat_router
from app.api.v1.conversations import router as conversations_router
from app.api.v1.messages import router as messages_router
from app.core.errors import DomainError
from app.core.llm.registry import registry
from app.core.telemetry.logging import configure_logging
from app.core.telemetry.middleware import RequestIDMiddleware
from app.db.session import engine

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Startup: configure logging. Shutdown: dispose the DB connection pool.

    Why disposing the pool matters: skipping it leaks open connections past
    process exit — harmless with --reload in dev, but in prod it delays
    container shutdown until postgres times the connections out itself.
    """
    configure_logging()
    logger.info("startup")
    yield
    await engine.dispose()
    logger.info("shutdown")


app = FastAPI(title="AgentOS", lifespan=lifespan)
app.add_middleware(RequestIDMiddleware)
app.include_router(conversations_router, prefix="/api/v1")
app.include_router(messages_router, prefix="/api/v1")
app.include_router(chat_router, prefix="/api/v1")


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

    # WHY this can't meaningfully fail: `registry` is loaded at import time
    # (app.core.llm.registry) — a malformed registry.yaml would have crashed
    # the process before it ever got this far. `len() == 0` is the only
    # state left to catch (an empty but structurally valid file).
    checks["registry"] = "ok" if len(registry) > 0 else "empty"

    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "unhealthy", "checks": checks},
    )
