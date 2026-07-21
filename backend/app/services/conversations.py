"""Conversation CRUD (API_CONTRACT.md §5.2).

Role: orchestration for creating, listing, reading, updating, and soft-deleting
conversations. No fastapi import (ARCHITECTURE.md's layering rule).
Called by: app/api/v1/conversations.py. Calls app.models.conversation,
app.core.ids, app.core.errors.
Gotcha: every read filters on `user_id` AND `deleted_at IS NULL` — a missing,
another user's, or soft-deleted conversation all look identical to the
caller (NotFoundError -> 404), per §1's "do not leak existence."
See: docs/API_CONTRACT.md#52-conversations
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.ids import new_id
from app.models.conversation import Conversation as ConversationModel
from app.schemas.conversation import Conversation, ConversationCreate, ConversationUpdate
from app.schemas.pagination import ConversationList, Pagination


def _to_schema(row: ConversationModel) -> Conversation:
    # WHY this can't be `Conversation.model_validate(row, from_attributes=True)`:
    # the ORM attribute is `metadata_`, not `metadata` (see
    # app/models/conversation.py's gotcha) — from_attributes looks up
    # `row.metadata`, which resolves to SQLAlchemy's own MetaData registry,
    # not the JSONB dict.
    return Conversation(
        id=row.id,
        title=row.title,
        system_prompt=row.system_prompt,
        default_model=row.default_model,
        default_params=row.default_params,
        metadata=row.metadata_,
        message_count=row.message_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _get_row(db: AsyncSession, user_id: str, conversation_id: str) -> ConversationModel:
    stmt = select(ConversationModel).where(
        ConversationModel.id == conversation_id,
        ConversationModel.user_id == user_id,
        ConversationModel.deleted_at.is_(None),
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFoundError(
            f"Conversation {conversation_id} not found.",
            code="conversation.not_found",
        )
    return row


async def create_conversation(
    db: AsyncSession, user_id: str, data: ConversationCreate
) -> Conversation:
    """Create a conversation. `title` stays null until the first exchange completes."""
    row = ConversationModel(
        id=new_id("conv"),
        user_id=user_id,
        title=data.title,
        system_prompt=data.system_prompt,
        default_model=data.default_model,
        default_params=data.default_params,
        metadata_=data.metadata,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _to_schema(row)


async def list_conversations(
    db: AsyncSession,
    user_id: str,
    *,
    limit: int,
    cursor: str | None,
    order: str,
) -> ConversationList:
    """Cursor-paginated list, newest-first by default (§5.2).

    WHY the cursor is just the last-seen id: resource IDs are uuid7 hex
    (core/ids.py), which sort chronologically as plain strings — no separate
    `created_at` sort key or offset/limit is needed.
    """
    stmt = select(ConversationModel).where(
        ConversationModel.user_id == user_id,
        ConversationModel.deleted_at.is_(None),
    )
    descending = order == "desc"
    if cursor is not None:
        stmt = stmt.where(
            ConversationModel.id < cursor if descending else ConversationModel.id > cursor
        )
    stmt = stmt.order_by(ConversationModel.id.desc() if descending else ConversationModel.id.asc())
    # WHY limit + 1: fetching one extra row is the cheapest way to know
    # `has_more` without a second COUNT query.
    stmt = stmt.limit(limit + 1)

    rows = list((await db.execute(stmt)).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]

    return ConversationList(
        data=[_to_schema(row) for row in rows],
        pagination=Pagination(
            next_cursor=rows[-1].id if has_more else None,
            has_more=has_more,
            limit=limit,
        ),
    )


async def get_conversation(db: AsyncSession, user_id: str, conversation_id: str) -> Conversation:
    row = await _get_row(db, user_id, conversation_id)
    return _to_schema(row)


async def update_conversation(
    db: AsyncSession, user_id: str, conversation_id: str, data: ConversationUpdate
) -> Conversation:
    """Partial update: an omitted field is left alone; an explicit `null` clears it.

    WHY `exclude_unset`, not just skipping `None` values: `None` is a valid
    value for e.g. `system_prompt` (clear it) — `exclude_unset` is the only
    way to tell "field omitted" apart from "field explicitly set to null."
    """
    row = await _get_row(db, user_id, conversation_id)
    updates = data.model_dump(exclude_unset=True)
    for field, value in updates.items():
        if field == "metadata":
            row.metadata_ = value
        else:
            setattr(row, field, value)
    await db.commit()
    await db.refresh(row)
    return _to_schema(row)


async def delete_conversation(db: AsyncSession, user_id: str, conversation_id: str) -> None:
    """Soft delete — sets `deleted_at`, never issues a SQL DELETE (§5.2)."""
    row = await _get_row(db, user_id, conversation_id)
    # WHY .replace(tzinfo=None): every timestamp column here is TIMESTAMP
    # WITHOUT TIME ZONE (default for a plain `Mapped[datetime]`) — asyncpg
    # rejects a tz-aware value against a naive column. The value is still
    # UTC; only the tzinfo marker is dropped.
    row.deleted_at = datetime.now(UTC).replace(tzinfo=None)
    await db.commit()
