"""Cursor pagination schemas (API_CONTRACT.md §5.2, §5.3).

Role: the wire shape wrapping a paginated `data` list. `ConversationList` and
`MessageList` share the same `Pagination` shape but wrap different item types.
Called by: app/api/v1/conversations.py, app/api/v1/messages.py. Calls
app.schemas.conversation, app.schemas.message.
See: docs/API_CONTRACT.md#52-conversations
"""

from pydantic import BaseModel, ConfigDict

from app.schemas.conversation import Conversation
from app.schemas.message import Message


class Pagination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    next_cursor: str | None
    has_more: bool
    limit: int


class ConversationList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: list[Conversation]
    pagination: Pagination


class MessageList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: list[Message]
    pagination: Pagination
