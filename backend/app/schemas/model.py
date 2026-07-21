"""Model registry entry schema (API_CONTRACT.md §4).

Role: wire shape for `GET /api/v1/models`. Field names and nesting mirror
the contract's worked example exactly.
Called by: app/api/v1/models.py, app/services/models.py. Calls nothing
internal.
Gotcha: `Capabilities`/`Pricing` here look identical to
`app.core.llm.catalog`'s same-named classes, but are a deliberate, separate
definition, not an import of them — see this package's own README ("A leaf
package, not a rung above core/llm/"). `catalog.py`'s versions are internal
`catalog.yaml` validation types; these are the public wire shape. Two call
sites happening to want the same fields today isn't a reason to let
`schemas/` depend upward on `core/llm/`.
Gotcha: `capabilities`, `pricing`, `context_window`, `max_output_tokens` are
all nullable — a model discovered live from a provider but absent from the
curated catalog has no honest non-null value for any of them (see
core/llm/registry.py's ModelEntry.from_live()). `available` is never null:
it's computed purely from whether the provider's API key is configured,
independent of curation.
See: docs/API_CONTRACT.md#4-model-registry
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class Capabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    streaming: bool
    tools: bool
    parallel_tool_calls: bool
    vision: bool
    json_mode: bool
    structured_output: bool
    reasoning: bool
    prompt_caching: bool
    system_prompt: bool


class Pricing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_per_mtok_usd: str
    output_per_mtok_usd: str
    cache_read_per_mtok_usd: str | None = None


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    provider: str
    display_name: str
    family: str
    context_window: int | None
    max_output_tokens: int | None
    capabilities: Capabilities | None
    pricing: Pricing | None
    available: bool
    deprecated_at: datetime | None


class ModelList(BaseModel):
    """WHY no `pagination` field, unlike `ConversationList`/`MessageList`:
    §4's own worked example response is just `{"data": [...]}` — the
    registry is small and static, not something a client pages through."""

    model_config = ConfigDict(extra="forbid")

    data: list[Model]
