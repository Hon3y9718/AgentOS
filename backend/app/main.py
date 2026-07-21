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
Gotcha: this is also the one place fastapi-users' plain HTTPExceptions
(app/api/v1/auth.py) get bridged into the §2 error envelope — see
http_exception_handler below and docs/DECISIONS/0003 Auth Layering.md.
See: docs/API_CONTRACT.md#51-health
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler as default_http_exception_handler
from fastapi.responses import JSONResponse, Response
from fastapi_users.router import ErrorCode
from sqlalchemy import text

from app.api.v1.auth import router as auth_router
from app.api.v1.chat import router as chat_router
from app.api.v1.conversations import router as conversations_router
from app.api.v1.messages import router as messages_router
from app.api.v1.models import router as models_router
from app.config import settings
from app.core.errors import ConflictError, DomainError, RequestValidationError, UnauthenticatedError
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
app.include_router(auth_router, prefix="/api/v1")
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


def _map_fastapi_users_exception(exc: HTTPException) -> DomainError | None:
    """Translate fastapi-users' known HTTPException shapes into DomainError.

    Returns None for anything not recognized (e.g. Starlette's own 404 for
    an unmatched route) — the caller falls back to FastAPI's default
    handling for those, unchanged.
    """
    detail: Any = exc.detail
    if isinstance(detail, dict):
        # WHY only this one dict shape is checked: REGISTER_INVALID_PASSWORD
        # is the only fastapi-users error whose detail is a dict (see
        # get_register_router's source) — {"code": ..., "reason": ...}.
        # RESET_PASSWORD_INVALID_PASSWORD/UPDATE_USER_INVALID_PASSWORD share
        # the same shape but no router exposing them is mounted (see
        # app/api/v1/auth.py) — matched anyway for robustness if one ever is.
        if detail.get("code") in (
            ErrorCode.REGISTER_INVALID_PASSWORD,
            ErrorCode.RESET_PASSWORD_INVALID_PASSWORD,
            ErrorCode.UPDATE_USER_INVALID_PASSWORD,
        ):
            return RequestValidationError(
                str(detail.get("reason") or "Invalid password."),
                code="auth.invalid_password",
            )
        return None
    if detail == ErrorCode.REGISTER_USER_ALREADY_EXISTS:
        return ConflictError("An account with this email already exists.", code="auth.email_taken")
    if detail == ErrorCode.LOGIN_BAD_CREDENTIALS:
        return UnauthenticatedError("Incorrect email or password.", code="auth.bad_credentials")
    if exc.status_code == 401:
        # WHY matched on status code alone, no ErrorCode string: fastapi_users'
        # Authenticator (behind current_active_user, i.e. every CurrentUser
        # use — app/api/v1/deps.py) raises HTTPException(401, detail=None)
        # uniformly for a missing, malformed, expired, or otherwise
        # unresolvable token (confirmed by reading authenticator.py's
        # _authenticate() and JWTStrategy.read_token()) — there is no
        # ErrorCode attached to that path.
        return UnauthenticatedError("Missing or invalid access token.", code="auth.invalid_token")
    return None


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> Response:
    """Bridges fastapi-users' plain HTTPExceptions into the §2 envelope.

    fastapi-users' routers (app/api/v1/auth.py) and its Authenticator
    dependency (behind CurrentUser) raise plain HTTPException with
    library-specific detail shapes and status codes that don't match
    API_CONTRACT §2 (e.g. login failure is 400, not 401) — this mirrors
    domain_error_handler immediately above for that one library's
    exceptions. Anything not recognized (Starlette's own 404 for an
    unmatched route, 405 for a wrong method, etc.) falls through to
    FastAPI's default HTTPException handling, completely unchanged — this
    is deliberately narrow, not a catch-all. See docs/DECISIONS/0003.
    """
    domain_exc = _map_fastapi_users_exception(exc)
    if domain_exc is None:
        return await default_http_exception_handler(request, exc)
    request_id = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=domain_exc.http_status, content={"error": domain_exc.to_envelope(request_id)}
    )


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
