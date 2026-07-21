"""Message schemas (API_CONTRACT.md §3.3, §5.3).

Role: wire shape for a message. Read-only — messages are produced by the
chat endpoint (§5.4), not a generic POST body, so there is no MessageCreate.
Called by: app/api/v1/messages.py. Calls app.schemas.content_block.
See: docs/API_CONTRACT.md#33-message
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.content_block import ContentBlock

Role = Literal["user", "assistant"]
MessageStatus = Literal["pending", "streaming", "complete", "incomplete", "failed"]
StopReason = Literal[
    "end_turn", "max_tokens", "tool_use", "stop_sequence", "content_filter", "error", "cancelled"
]


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    # WHY str, not float: API_CONTRACT.md — "cost_usd is a decimal string,
    # not a float. Money never rides a float."
    cost_usd: str


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    conversation_id: str
    role: Role
    content: list[ContentBlock]
    status: MessageStatus
    model: str | None = None
    stop_reason: StopReason | None = None
    usage: Usage | None = None
    created_at: datetime
    completed_at: datetime | None = None


class MessageDeleteResult(BaseModel):
    """Response for the truncate-delete endpoint (§5.3) — reports what was removed."""

    model_config = ConfigDict(extra="forbid")

    deleted_message_ids: list[str]
    count: int
