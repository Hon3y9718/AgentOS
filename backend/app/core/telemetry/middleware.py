"""Request-ID middleware.

Role: assign/propagate a request ID, bind it to the structlog context for the
request's duration, and set X-Request-Id on every response (API_CONTRACT §1).
Called by: main.py via app.add_middleware. Calls nothing internal.
Gotcha: uses structlog.contextvars (not a plain global) — each request gets an
isolated context even under concurrent async requests on the same worker.
See: docs/API_CONTRACT.md#1-authentication
"""

import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Binds a per-request ID to structlog context and the response headers."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # WHY uuid4, not the UUIDv7 the contract specifies for resource IDs
        # (conv_/msg_/run_): stdlib `uuid` has no uuid7 on 3.12 (lands in
        # 3.14), and request IDs don't need the sortability that motivates
        # v7 for paginated resources. Flagged in BUILD_LOG — revisit if a
        # uuid7 dependency gets added for resource IDs anyway.
        request_id = f"req_{uuid.uuid4().hex}"
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        # WHY also on request.state, not just the structlog contextvar:
        # BaseHTTPMiddleware runs the downstream app in a separate anyio
        # task, and exception handlers run outside that task once an error
        # propagates. request.state is the same object regardless of task
        # boundaries, so it's the reliable read path for main.py's handler.
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response
