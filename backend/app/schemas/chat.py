"""Chat request/response schemas (API_CONTRACT.md §5.4).

Role: wire shape for the chat endpoint. `ChatRequest` is the only schema in
this package with a "fall back to conversation defaults" contract — the
service, not this file, resolves those fallbacks (schemas/ must not know
about the DB, per ARCHITECTURE.md's package table).
Called by: app/api/v1/chat.py. Calls app.schemas.content_block, app.schemas.message.
Gotcha: `stream` is accepted but currently inert — this slice only serves
Accept: application/json (§5.4's non-streaming variant); the router rejects
Accept: text/event-stream (§5.5) with a clean error rather than honoring
`stream: true` from the body. Dispatch is on the Accept header, matching
§5.5's own framing ("Accept: text/event-stream on the endpoint above").
See: docs/API_CONTRACT.md#54-chat--the-core-endpoint
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.content_block import ContentBlock
from app.schemas.message import Message


class ChatParams(BaseModel):
    """Normalized param overrides for one turn — every field optional so the
    service can distinguish "omitted" (fall back to conversation defaults)
    from "explicitly set" via model_dump(exclude_unset=True), same technique
    ConversationUpdate already uses."""

    model_config = ConfigDict(extra="forbid")

    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    stop_sequences: list[str] | None = None
    reasoning_effort: str | None = None
    response_format: dict[str, object] | None = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # WHY max_length=100: §6's "blocks per message" hard limit, enforced
    # here rather than in the router (CLAUDE.md convention — field-level
    # constraints mirror §6, not ad hoc checks in api/v1/).
    content: list[ContentBlock] = Field(min_length=1, max_length=100)
    model: str | None = None
    params: ChatParams | None = None
    # WHY list[str], not JSON Schema: §5.4 — "tools names tools from the
    # server-side registry. Clients do not send JSON Schema." Non-empty is
    # rejected this slice (§5.6 doesn't exist yet) — see chat.py service.
    tools: list[str] = Field(default_factory=list)
    tool_choice: Literal["auto", "none", "required"] | dict[str, str] = "auto"
    stream: bool = False


class ChatResponse(BaseModel):
    """§5.4's non-streaming (Accept: application/json) response body."""

    model_config = ConfigDict(extra="forbid")

    user_message: Message
    assistant_message: Message
