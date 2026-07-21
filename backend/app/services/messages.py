"""Message list + truncate-delete (API_CONTRACT.md §5.3).

Role: orchestration for reading a conversation's message history and for the
truncate-delete that backs client-side "edit and resend"/"regenerate". No
fastapi import (ARCHITECTURE.md's layering rule).
Called by: app/api/v1/messages.py. Calls app.models.message,
app.services.conversations, app.core.errors.
Gotcha: there is no `create_message` here — messages are only ever produced
by the chat endpoint (§5.4, `app/services/chat.py`). This module is read +
truncate only; `row_to_schema()` is exported (not `_`-prefixed) specifically
so `chat.py` can build the same wire `Message` shape for the messages it
persists, instead of duplicating this mapping.
See: docs/API_CONTRACT.md#53-messages
"""

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models.message import Message as MessageModel
from app.schemas.message import Message, MessageDeleteResult
from app.schemas.pagination import MessageList, Pagination
from app.services import conversations as conversations_service


def row_to_schema(row: MessageModel, *, include_reasoning: bool) -> Message:
    # WHY filter here, not in the DB query: `content` is a JSONB list, not a
    # normalized table (app/models/message.py's gotcha) — there is no column
    # to filter on, so reasoning blocks are dropped from the already-fetched
    # list before it's validated into the Message schema (§3.1's default).
    content = row.content
    if not include_reasoning:
        content = [block for block in content if block.get("type") != "reasoning"]
    return Message(
        id=row.id,
        conversation_id=row.conversation_id,
        role=row.role,
        content=content,
        status=row.status,
        model=row.model,
        stop_reason=row.stop_reason,
        usage=row.usage,
        created_at=row.created_at,
        completed_at=row.completed_at,
    )


async def list_messages(
    db: AsyncSession,
    user_id: str,
    conversation_id: str,
    *,
    limit: int,
    cursor: str | None,
    order: str,
    include_reasoning: bool,
) -> MessageList:
    """Cursor-paginated list, chronological (`asc`) by default (§5.3).

    WHY `asc` here but `desc` for conversations (§5.2): a transcript reads
    oldest-first — the chat UI would otherwise pass `?order=asc` on every
    single call.
    """
    # WHY call the conversations service instead of querying directly: reuses
    # the exact same "user_id match AND deleted_at IS NULL" scoping (§1's
    # "do not leak existence") without duplicating that query here. The
    # returned Conversation schema is discarded — only the 404-if-invalid
    # side effect matters.
    await conversations_service.get_conversation(db, user_id, conversation_id)

    stmt = select(MessageModel).where(MessageModel.conversation_id == conversation_id)
    descending = order == "desc"
    if cursor is not None:
        stmt = stmt.where(MessageModel.id < cursor if descending else MessageModel.id > cursor)
    stmt = stmt.order_by(MessageModel.id.desc() if descending else MessageModel.id.asc())
    # WHY limit + 1: fetching one extra row is the cheapest way to know
    # `has_more` without a second COUNT query.
    stmt = stmt.limit(limit + 1)

    rows = list((await db.execute(stmt)).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]

    return MessageList(
        data=[row_to_schema(row, include_reasoning=include_reasoning) for row in rows],
        pagination=Pagination(
            next_cursor=rows[-1].id if has_more else None,
            has_more=has_more,
            limit=limit,
        ),
    )


async def delete_message_and_after(
    db: AsyncSession, user_id: str, conversation_id: str, message_id: str
) -> MessageDeleteResult:
    """Delete `message_id` and every later message in the conversation (§5.3).

    WHY a hard DELETE, not a soft delete like conversations: messages have no
    `deleted_at` column (app/models/message.py) — nothing in the contract
    treats a truncated message as recoverable history.
    """
    await conversations_service.get_conversation(db, user_id, conversation_id)

    target = (
        await db.execute(
            select(MessageModel).where(
                MessageModel.id == message_id,
                MessageModel.conversation_id == conversation_id,
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise NotFoundError(
            f"Message {message_id} not found.",
            code="message.not_found",
        )

    # WHY id, not created_at, for the ">=" boundary: resource IDs are uuid7
    # hex (core/ids.py), which sort chronologically as plain strings — same
    # reasoning as the cursor-pagination queries above.
    ids_stmt = (
        select(MessageModel.id)
        .where(
            MessageModel.conversation_id == conversation_id,
            MessageModel.id >= target.id,
        )
        .order_by(MessageModel.id.asc())
    )
    deleted_ids = list((await db.execute(ids_stmt)).scalars().all())

    await db.execute(delete(MessageModel).where(MessageModel.id.in_(deleted_ids)))
    await db.commit()

    return MessageDeleteResult(deleted_message_ids=deleted_ids, count=len(deleted_ids))
