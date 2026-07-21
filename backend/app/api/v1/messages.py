"""Messages router (API_CONTRACT.md §5.3).

Role: HTTP surface for a conversation's message history — cursor list and
truncate-delete. Thin — validates, calls app.services.messages, returns a
schema.
Called by: app/main.py (included under /api/v1). Calls app.services.messages,
app.api.v1.deps, app.db.session.
Gotcha: nested under /conversations/{conversation_id}/messages — both routes
404 (never 403) if the conversation doesn't resolve for the caller, enforced
by the service delegating to app.services.conversations (§1).
See: docs/API_CONTRACT.md#53-messages
"""

from typing import Literal

from fastapi import APIRouter, Query

from app.api.v1.deps import CurrentUser
from app.db.session import DbSession
from app.schemas.message import MessageDeleteResult
from app.schemas.pagination import MessageList
from app.services import messages as service

router = APIRouter(prefix="/conversations/{conversation_id}/messages", tags=["messages"])


@router.get("", response_model=MessageList)
async def list_messages(
    conversation_id: str,
    user_id: CurrentUser,
    db: DbSession,
    # WHY limit capped at 100, same as conversations' list: an implementation
    # choice, not a §6 contract limit.
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
    # WHY default "asc" here but "desc" for conversations: §5.3 — a
    # transcript reads oldest-first.
    order: Literal["asc", "desc"] = Query(default="asc"),
    include_reasoning: bool = Query(default=False),
) -> MessageList:
    return await service.list_messages(
        db,
        user_id,
        conversation_id,
        limit=limit,
        cursor=cursor,
        order=order,
        include_reasoning=include_reasoning,
    )


@router.delete("/{message_id}", response_model=MessageDeleteResult)
async def delete_message(
    conversation_id: str,
    message_id: str,
    user_id: CurrentUser,
    db: DbSession,
) -> MessageDeleteResult:
    return await service.delete_message_and_after(db, user_id, conversation_id, message_id)
