"""Normalized LLM request/event types (API_CONTRACT.md §5.4, §5.5, ADR-0002).

Role: the shape every provider adapter translates into (LLMRequest) and out
of (LLMEvent). Zero knowledge of conversations, users, or persistence — this
package is reused by the future agent runtime and background jobs, not just
HTTP requests (ARCHITECTURE.md).
Called by: app/core/llm/adapter.py (the interface), app/core/llm/anthropic_adapter.py
(and every future adapter).
Calls: app.schemas.content_block, app.schemas.message — ADR-0002 decision 2:
reuse §3.1's block shapes and §3.3's StopReason rather than duplicate them
inside this package. `Usage` itself is NOT reused (see LLMUsage below).
Gotcha: deliberately excludes `message_start`, `message_stop`, and `ping`
(§5.5). The first two carry message/run IDs this package has no business
knowing about; `ping` is a stream-transport keepalive concern owned by
whichever layer frames SSE (api/v1/, per ARCHITECTURE.md) — see ADR-0002
decision 6. Every adapter's stream() yields only block-level events plus one
terminal MessageDelta.
See: docs/DECISIONS/0002 Provider Abstraction.md
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.content_block import ContentBlock
from app.schemas.message import StopReason


class LLMMessage(BaseModel):
    """One turn of normalized history handed to an adapter.

    Not the same type as app.schemas.message.Message — that one also carries
    persistence fields (id, status, timestamps) this package has no business
    knowing about (ARCHITECTURE.md: "core/llm knows nothing about
    conversations").
    """

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: list[ContentBlock]


class ToolDefinition(BaseModel):
    """A server-owned tool definition (§5.6), resolved by the caller before
    it reaches an adapter — core/llm/ never looks up a tool by name."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    input_schema: dict[str, object]


class LLMParams(BaseModel):
    """Normalized request params (§5.4). Provider-specific knobs are never
    passed through — a param a given adapter's provider doesn't support is
    dropped by that adapter, not forwarded and not silently ignored upward."""

    model_config = ConfigDict(extra="forbid")

    temperature: float | None = None
    max_tokens: int
    top_p: float | None = None
    stop_sequences: list[str] = Field(default_factory=list)
    reasoning_effort: str | None = None
    response_format: dict[str, object] | None = None


class LLMRequest(BaseModel):
    """The one input shape every adapter's stream() accepts."""

    model_config = ConfigDict(extra="forbid")

    # WHY a bare model name, not "provider:model": the registry (§4) strips
    # the provider prefix and resolves it to a specific adapter before this
    # is constructed — by the time an adapter sees a request, which provider
    # it's talking to is already decided by which adapter got called.
    model: str
    system_prompt: str | None
    messages: list[LLMMessage]
    params: LLMParams
    tools: list[ToolDefinition] = Field(default_factory=list)
    tool_choice: Literal["auto", "none", "required"] | dict[str, str] = "auto"


# --- normalized event union (§5.5, minus message_start/message_stop/ping) ---


class TextBlockStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["text"] = "text"


class ToolUseBlockStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str


class ReasoningBlockStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["reasoning"] = "reasoning"


BlockStart = Annotated[
    TextBlockStart | ToolUseBlockStart | ReasoningBlockStart,
    Field(discriminator="type"),
]


class ContentBlockStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int
    block: BlockStart


class TextDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["text_delta"] = "text_delta"
    text: str


class InputJsonDelta(BaseModel):
    """A fragment of a tool_use block's JSON arguments (§5.5). Fragments are
    concatenated by the caller and parsed only after content_block_stop —
    partial JSON is never valid and adapters must not attempt to parse it."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["input_json_delta"] = "input_json_delta"
    partial_json: str


class ReasoningDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["reasoning_delta"] = "reasoning_delta"
    text: str


Delta = Annotated[
    TextDelta | InputJsonDelta | ReasoningDelta,
    Field(discriminator="type"),
]


class ContentBlockDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int
    delta: Delta


class ContentBlockStop(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int


class LLMUsage(BaseModel):
    """Raw token counts an adapter can observe directly from its provider.

    WHY not app.schemas.message.Usage: that type requires `cost_usd`, a
    decimal computed from token counts × the registry's per-model pricing
    (API_CONTRACT §3.3). ARCHITECTURE.md's request lifecycle assigns
    "computed cost" to the service, not the adapter — core/llm/ has no
    business reaching into registry pricing data to price its own output.
    The service combines this with a resolved RegistryEntry.pricing to build
    the wire Usage when it persists/returns the message.
    """

    model_config = ConfigDict(extra="forbid")

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0


class MessageDelta(BaseModel):
    """The terminal event of a stream() call — every adapter yields exactly
    one of these, last, after all content-block events."""

    model_config = ConfigDict(extra="forbid")
    stop_reason: StopReason
    usage: LLMUsage


LLMEvent = ContentBlockStart | ContentBlockDelta | ContentBlockStop | MessageDelta
