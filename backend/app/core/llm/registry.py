"""Static model registry loader (API_CONTRACT.md §4, ADR-0002 decision 5).

Role: parses and validates registry.yaml once, at import time — a malformed
file crashes the process before uvicorn binds a port, the same failure mode
app/config.py uses for a missing DATABASE_URL. Never re-read from providers
at runtime (§4): a provider outage must not change which models this API
claims to support.
Called by: app/main.py (/health/ready's registry check), app/services/chat.py
(once it exists, to resolve a "provider:model" id to capabilities before
calling an adapter).
Calls: app.config (the "is a key configured" half of `available`),
app.core.errors.
Gotcha: `available` is computed by ModelRegistry.is_available(), not stored
as a field on RegistryEntry — §4 says it "reflects whether the required API
key is configured plus the last health probe result," which is runtime
state, not something registry.yaml can author.
See: docs/API_CONTRACT.md#4-model-registry
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

from app.config import settings
from app.core.errors import InvalidRequestError

_REGISTRY_PATH = Path(__file__).parent / "registry.yaml"

Provider = Literal["openai", "anthropic", "together", "groq", "gemini"]


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

    # WHY str, not float: CLAUDE.md — money is a decimal string, never a
    # float, same rule app.schemas.message.Usage.cost_usd follows.
    input_per_mtok_usd: str
    output_per_mtok_usd: str
    cache_read_per_mtok_usd: str | None = None


class RegistryEntry(BaseModel):
    """One row of registry.yaml — mirrors §4's model-object shape.

    `available` and `deprecated_at` from §4's wire shape are intentionally
    absent: `available` is computed (see module docstring); no model here is
    deprecated yet, so there is nothing to author for that field today.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    provider: Provider
    display_name: str
    family: str
    context_window: int
    max_output_tokens: int
    capabilities: Capabilities
    pricing: Pricing

    @property
    def bare_model_id(self) -> str:
        """The model name without its "provider:" prefix.

        WHY this exists: `core/llm/types.py`'s `LLMRequest.model` is
        documented as a bare name (ADR-0002) — "the registry strips the
        provider prefix before resolving which adapter to call." This is
        that stripping, centralized here instead of every caller
        hand-rolling `entry.id.split(":", 1)[1]`.
        """
        return self.id.removeprefix(f"{self.provider}:")


class _RegistryFile(BaseModel):
    """The whole parsed registry.yaml — a validation-only shape, not exposed
    outside this module."""

    model_config = ConfigDict(extra="forbid")

    models: list[RegistryEntry]


class ModelRegistry:
    """In-memory lookup from a `provider:model` id to its registry entry."""

    def __init__(self, entries: list[RegistryEntry]) -> None:
        self._by_id = {entry.id: entry for entry in entries}

    def resolve(self, model_id: str) -> RegistryEntry:
        """Look up a `provider:model` id.

        Raises:
            InvalidRequestError: `model_id` isn't in the registry (§4: "Any
                request naming a model absent from the registry fails fast
                with invalid_request").
        """
        entry = self._by_id.get(model_id)
        if entry is None:
            raise InvalidRequestError(
                f"Unknown model {model_id!r}.",
                code="model.not_found",
                details={"model": model_id},
            )
        return entry

    def is_available(self, entry: RegistryEntry) -> bool:
        """Whether the entry's provider is currently usable.

        WHY not a stored field: see module docstring. There is no health
        probe yet (GET /api/v1/providers/health, §5.1), so this is only the
        key-configured half of §4's definition for now.
        """
        return getattr(settings, f"{entry.provider}_api_key") is not None

    def __iter__(self) -> Iterator[RegistryEntry]:
        return iter(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)


def _load_registry() -> ModelRegistry:
    raw = _REGISTRY_PATH.read_text()
    parsed = _RegistryFile.model_validate(yaml.safe_load(raw))
    return ModelRegistry(parsed.models)


# WHY module-level, not a lazy factory: same reasoning as app.config.settings
# — importing this module is the startup path, so a malformed registry.yaml
# fails loudly before the process ever binds a port (ADR-0002 decision 5).
registry = _load_registry()
