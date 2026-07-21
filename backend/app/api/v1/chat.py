"""Chat router (API_CONTRACT.md §5.4, §5.5).

Role: HTTP surface for the core chat turn, both response shapes. Thin —
validates, calls app.services.chat, returns a schema or a stream. Shares its
URL path with app/api/v1/messages.py (same resource, different HTTP method)
but is kept as its own router module, matching how app/services/ also splits
chat.py from messages.py.
Called by: app/main.py (included under /api/v1). Calls app.services.chat,
app.api.v1.deps, app.db.session.
Gotcha: for `Accept: text/event-stream`, `service.prepare_stream()` is
`await`ed here — outside the StreamingResponse — specifically so a
`DomainError` it raises (bad conversation, bad model, idempotency conflict)
still produces a normal pre-stream HTTP error via main.py's exception
handler. Only `service.emit_stream()`, called after that succeeds, becomes
the response body generator. See app/services/chat.py's module docstring.
See: docs/API_CONTRACT.md#54-chat--the-core-endpoint
"""

from fastapi import APIRouter, Header, Request, status
from fastapi.responses import StreamingResponse

from app.api.v1.deps import CurrentUser
from app.db.session import DbSession
from app.schemas.chat import ChatRequest, ChatResponse
from app.services import chat as service

router = APIRouter(prefix="/conversations/{conversation_id}/messages", tags=["chat"])


@router.post("", response_model=ChatResponse, status_code=status.HTTP_201_CREATED)
async def create_chat_message(
    conversation_id: str,
    body: ChatRequest,
    user_id: CurrentUser,
    db: DbSession,
    request: Request,
    # WHY Header(alias=...), no default: §5.4 — "Idempotency-Key is required
    # for this endpoint." A missing header is a 422 (FastAPI's own request
    # validation), same as any other required-field violation.
    idempotency_key: str = Header(alias="Idempotency-Key"),
    accept: str | None = Header(default=None),
) -> ChatResponse | StreamingResponse:
    if accept is not None and "text/event-stream" in accept:
        plan = await service.prepare_stream(db, user_id, conversation_id, idempotency_key, body)
        return StreamingResponse(
            service.emit_stream(
                db,
                plan,
                request_id=getattr(request.state, "request_id", None),
                is_disconnected=request.is_disconnected,
            ),
            media_type="text/event-stream",
            # WHY these three headers, verbatim from §5.5: Cache-Control
            # stops an intermediary from caching a live stream;
            # X-Accel-Buffering: no is not optional behind nginx — without
            # it the whole stream gets buffered into one blocking response
            # in production; Connection: keep-alive matches a long-lived SSE
            # connection, not a short request/response cycle.
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )
    return await service.create_chat_message(db, user_id, conversation_id, idempotency_key, body)
