"""Model registry endpoint (API_CONTRACT.md §4).

Role: filters app.core.llm.registry's live ModelRegistry into the wire
`Model`/`ModelList` shape. No DB session, no fastapi import.
Called by: app/api/v1/models.py. Calls app.core.llm.registry, app.schemas.model.
Gotcha: an unrecognized `provider` or `capability` filter value excludes
every model rather than raising `invalid_request` — see `_matches()`.
Gotcha: `refresh_if_stale()` (called first, below) is async and may hit
every configured provider's API — but it degrades gracefully and never
raises; a provider that's slow, down, or errors just means this response
serves that provider's last-known-good entries. See registry.py.
See: docs/API_CONTRACT.md#4-model-registry
"""

from app.core.llm.registry import ModelEntry, registry
from app.schemas.model import Capabilities, Model, ModelList, Pricing


def _to_schema(entry: ModelEntry) -> Model:
    return Model(
        id=entry.id,
        provider=entry.provider,
        display_name=entry.display_name,
        family=entry.family,
        context_window=entry.context_window,
        max_output_tokens=entry.max_output_tokens,
        capabilities=Capabilities(**entry.capabilities.model_dump())
        if entry.capabilities is not None
        else None,
        pricing=Pricing(**entry.pricing.model_dump()) if entry.pricing is not None else None,
        available=registry.is_available(entry),
        # WHY always None: no model, curated or live, carries a deprecation
        # date today — catalog.yaml has no such field, and no provider's
        # live model list reports one either.
        deprecated_at=None,
    )


def _matches(
    entry: ModelEntry,
    *,
    provider: str | None,
    capabilities: list[str],
    available: bool | None,
) -> bool:
    # WHY getattr with a False default, not a KeyError/ValueError on a typo'd
    # capability name: §4 documents `?capability=tools`/`?capability=vision`
    # as example filters but never defines behavior for an unrecognized
    # value. Treating it the same as "no model has this capability" (empty
    # result) is the simplest reading, and matches how an unrecognized
    # `provider` value below also just yields zero matches rather than 400.
    # This also covers `entry.capabilities is None` (a live-only model with
    # no curated data) for free: `getattr(None, name, False)` returns
    # False, the same "doesn't have it" answer a real-but-absent capability
    # would — no separate None-check needed.
    if provider is not None and entry.provider != provider:
        return False
    if available is not None and registry.is_available(entry) != available:
        return False
    return all(getattr(entry.capabilities, name, False) for name in capabilities)


async def list_models(
    *, provider: str | None, capabilities: list[str], available: bool | None
) -> ModelList:
    """Refresh-if-stale, then filter the registry into the §4 wire shape.

    Args:
        provider: exact-match filter (e.g. "groq"). None means unfiltered.
        capabilities: capability names that must ALL be true — ANDed, per §4
            ("multiple capabilities are ANDed").
        available: filter by computed availability (API key configured).

    Returns:
        Every registry entry matching all given filters, as `ModelList`.
    """
    await registry.refresh_if_stale()
    return ModelList(
        data=[
            _to_schema(entry)
            for entry in registry
            if _matches(entry, provider=provider, capabilities=capabilities, available=available)
        ]
    )
