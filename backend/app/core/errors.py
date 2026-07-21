"""Domain error taxonomy (API_CONTRACT.md §2).

Role: the only typed failure services may raise. The API layer's single exception
handler (main.py) maps these to the wire error envelope.
Called by: app/services/* once they exist. Calls nothing internal.
Gotcha: must import nothing from fastapi/starlette (ARCHITECTURE.md forbids it here)
— http_status is plain data, not a framework Response.
See: docs/API_CONTRACT.md#2-error-envelope
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for every error a service may raise.

    Args:
        message: human-facing text; may change freely, unlike `type`.
        code: stable dotted machine code, e.g. "provider.rate_limited".
        details: free-form extra context (e.g. {"provider": "groq"}). Must
            never carry secrets — it is sent to the client verbatim.
        retry_after_seconds: only meaningful when `retryable` is True.

    Why class attributes for type/http_status/retryable rather than
    constructor args: each concrete subclass is then self-describing, and a
    service can't accidentally pair the wrong HTTP status with an error type.
    """

    type: str = "internal_error"
    http_status: int = 500
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: str,
        details: dict[str, object] | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}
        self.retry_after_seconds = retry_after_seconds

    def to_envelope(self, request_id: str | None) -> dict[str, object]:
        """Build the §2 error envelope's inner `error` object.

        WHY this lives here, not duplicated at each call site: main.py's
        HTTP exception handler and app/services/chat.py's in-stream SSE
        error framing (§5.5 — an error after headers are already sent can't
        go through the normal exception-handler path) both need the exact
        same shape. One definition, not two that can drift.
        """
        envelope: dict[str, object] = {
            "type": self.type,
            "message": self.message,
            "code": self.code,
            "request_id": request_id,
            "retryable": self.retryable,
            "details": self.details,
        }
        if self.retry_after_seconds is not None:
            envelope["retry_after_seconds"] = self.retry_after_seconds
        return envelope


class InvalidRequestError(DomainError):
    type = "invalid_request"
    http_status = 400


class RequestValidationError(DomainError):
    # WHY not named `ValidationError`: that shadows pydantic's own
    # ValidationError, which services in this codebase will also see.
    type = "validation_error"
    http_status = 422


class UnauthenticatedError(DomainError):
    type = "unauthenticated"
    http_status = 401


class PermissionDeniedError(DomainError):
    type = "permission_denied"
    http_status = 403


class NotFoundError(DomainError):
    type = "not_found"
    http_status = 404


class ConflictError(DomainError):
    type = "conflict"
    http_status = 409


class PayloadTooLargeError(DomainError):
    type = "payload_too_large"
    http_status = 413


class RateLimitedError(DomainError):
    type = "rate_limited"
    http_status = 429
    retryable = True


class ContextLengthExceededError(DomainError):
    type = "context_length_exceeded"
    http_status = 422


class ContentFilteredError(DomainError):
    type = "content_filtered"
    http_status = 422


class ProviderError(DomainError):
    type = "provider_error"
    http_status = 502
    retryable = True


class ProviderUnavailableError(DomainError):
    type = "provider_unavailable"
    http_status = 503
    retryable = True


class InternalError(DomainError):
    type = "internal_error"
    http_status = 500
