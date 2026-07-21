"""Content block schemas (API_CONTRACT.md §3.1).

Role: the wire shape for message content — always a list of these blocks,
never a plain string. Discriminated on `type` (image sources on `kind`).
Called by: app/schemas/message.py. Calls nothing internal.
Gotcha: `tool_result` blocks must appear before any `text` block in a user
message (§3.1) — that ordering rule is not validated here, a single block's
shape is all this file knows about. It belongs to whoever assembles the list.
See: docs/API_CONTRACT.md#31-content-blocks
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class TextBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["text"] = "text"
    # WHY max_length here, on the one shared definition, rather than only on
    # a request schema: §6's "text block length: 1,000,000 chars" applies
    # regardless of whether the block came from a client or was persisted
    # from a provider response — this is the single place both paths share.
    text: str = Field(max_length=1_000_000)


class ImageSourceBase64(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["base64"] = "base64"
    media_type: str
    data: str


class ImageSourceUrl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["url"] = "url"
    url: str


class ImageSourceFileId(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["file_id"] = "file_id"
    file_id: str


ImageSource = Annotated[
    ImageSourceBase64 | ImageSourceUrl | ImageSourceFileId,
    Field(discriminator="kind"),
]


class ImageBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["image"] = "image"
    source: ImageSource


class ToolUseBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, object]


class ToolResultBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    is_error: bool = False
    # WHY list[TextBlock], not list[ContentBlock]: §3.1's example only shows
    # text inside a tool_result, and a self-referential union here would need
    # a forward ref for no contract-documented benefit. Widen if a provider
    # ever returns e.g. an image as a tool result.
    content: list[TextBlock]


class ReasoningBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["reasoning"] = "reasoning"
    text: str
    redacted: bool = False
    signature: str | None = None


ContentBlock = Annotated[
    TextBlock | ImageBlock | ToolUseBlock | ToolResultBlock | ReasoningBlock,
    Field(discriminator="type"),
]
