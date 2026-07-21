"""Conversations router (API_CONTRACT.md §5.2).

Role: HTTP surface for conversation CRUD. Thin — validates, calls
app.services.conversations, returns a schema.
Called by: app/main.py (included under /api/v1). Calls app.services.conversations,
app.api.v1.deps, app.db.session.
Gotcha: soft-deleted and other-users' conversations both 404, never 403 (§1) —
enforced by the service, which scopes every query to user_id AND deleted_at IS NULL.
See: docs/API_CONTRACT.md#52-conversations
"""

from typing import Literal

from fastapi import APIRouter, Query, status

from app.api.v1.deps import CurrentUser
from app.db.session import DbSession
from app.schemas.conversation import Conversation, ConversationCreate, ConversationUpdate
from app.schemas.pagination import ConversationList
from app.services import conversations as service

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.post("", response_model=Conversation, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: ConversationCreate,
    user_id: CurrentUser,
    db: DbSession,
) -> Conversation:
    return await service.create_conversation(db, user_id, body)


@router.get("", response_model=ConversationList)
async def list_conversations(
    user_id: CurrentUser,
    db: DbSession,
    # WHY limit capped at 100 as an implementation choice, not a §6 limit:
    # the contract documents `?limit=20` as an example but sets no hard
    # ceiling for this endpoint.
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
    order: Literal["asc", "desc"] = Query(default="desc"),
) -> ConversationList:
    return await service.list_conversations(db, user_id, limit=limit, cursor=cursor, order=order)


@router.get("/{conversation_id}", response_model=Conversation)
async def get_conversation(
    conversation_id: str,
    user_id: CurrentUser,
    db: DbSession,
) -> Conversation:
    return await service.get_conversation(db, user_id, conversation_id)


@router.patch("/{conversation_id}", response_model=Conversation)
async def update_conversation(
    conversation_id: str,
    body: ConversationUpdate,
    user_id: CurrentUser,
    db: DbSession,
) -> Conversation:
    return await service.update_conversation(db, user_id, conversation_id, body)


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: str,
    user_id: CurrentUser,
    db: DbSession,
) -> None:
    await service.delete_conversation(db, user_id, conversation_id)
