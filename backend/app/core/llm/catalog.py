"""Curated model catalog loader (API_CONTRACT.md §4, ADR-0002 decision 5 —
updated 2026-07-21, see docs/DECISIONS/0002 Provider Abstraction.md).

Role: parses and validates catalog.yaml once, at import time — a malformed
file crashes the process before uvicorn binds a port, the same
crash-loudly-at-boot pattern app/config.py uses for a missing DATABASE_URL.
This module supplies capabilities/pricing/display data for models verified
by hand; it is NOT the list of models the API can serve — that list is
discovered live, per provider, at runtime (see registry.py). A bad row here
is a checked-in operator mistake and stays crash-loud; a provider being
briefly unreachable is a different kind of failure and is handled entirely
in registry.py instead — two failure-handling policies, in two modules, on
purpose.
Called by: app/core/llm/registry.py (the only caller — everything else goes
through registry.ModelRegistry, never this module directly).
Calls: nothing internal.
See: docs/API_CONTRACT.md#4-model-registry
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

_CATALOG_PATH = Path(__file__).parent / "catalog.yaml"

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


class CatalogEntry(BaseModel):
    """One row of catalog.yaml — capabilities/pricing/display data for a
    model curated by hand. See registry.py's ModelEntry for the shape
    actually served over the wire, which merges this with live provider
    data."""

    model_config = ConfigDict(extra="forbid")

    id: str
    provider: Provider
    display_name: str
    family: str
    context_window: int
    max_output_tokens: int
    capabilities: Capabilities
    pricing: Pricing


class _CatalogFile(BaseModel):
    """The whole parsed catalog.yaml — a validation-only shape, not exposed
    outside this module."""

    model_config = ConfigDict(extra="forbid")

    models: list[CatalogEntry]


def _load_catalog() -> dict[str, CatalogEntry]:
    raw = _CATALOG_PATH.read_text()
    parsed = _CatalogFile.model_validate(yaml.safe_load(raw))
    return {entry.id: entry for entry in parsed.models}


# WHY module-level, not a lazy factory: importing this module is the startup
# path, so a malformed catalog.yaml fails loudly before the process ever
# binds a port — same reasoning app.config.settings already documents.
CATALOG: dict[str, CatalogEntry] = _load_catalog()
