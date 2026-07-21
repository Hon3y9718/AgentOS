"""Live model registry (API_CONTRACT.md §4, ADR-0002 decision 5 — updated
2026-07-21, see docs/DECISIONS/0002 Provider Abstraction.md).

Role: merges app.core.llm.catalog's curated enrichment data with each
configured provider's own live model list into one in-memory lookup from a
"provider:model" id to a ModelEntry.
Called by: app/main.py (startup refresh, /health/ready's registry check),
app/services/chat.py (resolve a "provider:model" id before calling an
adapter — synchronous, reads only the in-memory cache, never awaits or
touches httpx), app/services/models.py (GET /api/v1/models, which calls
refresh_if_stale() before serving).
Calls: app.config (API keys), app.core.errors, app.core.llm.catalog,
app.core.llm.adapter (ADAPTER_CLASSES, to call each provider's
list_models()).
Gotcha: ModelRegistry.__init__ seeds every catalog entry into the live
in-memory dict synchronously, unconditionally, with no network call. This
is deliberate, not an optimization: app/services/chat.py's hot path calls
resolve()/is_available() on every single chat message, and must resolve a
catalog-known model correctly from the instant this module imports —
independent of whether any live refresh has run yet, or ever succeeds.
Only refresh_if_stale() touches a provider over the network, and only
app/services/models.py and app/main.py's startup task ever call it.
Gotcha: a model discovered live but absent from the catalog has
`capabilities`/`pricing` as None, not fabricated defaults — see
ModelEntry.from_live(). `available` still only reflects whether that
provider's API key is configured (unchanged meaning from before this
update — no live health probe exists yet, see GET /api/v1/providers/health
in ROADMAP.md).
See: docs/API_CONTRACT.md#4-model-registry
"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import structlog

from app.config import settings
from app.core.errors import InvalidRequestError
from app.core.llm.adapter import ADAPTER_CLASSES, ProviderAdapter
from app.core.llm.catalog import CATALOG, Capabilities, CatalogEntry, Pricing, Provider
from app.core.llm.types import ProviderModel

logger = structlog.get_logger()

# WHY 5 minutes: short enough that a newly-released model shows up in a
# normal dev/test session without a restart, long enough that repeatedly
# opening GET /api/v1/models (e.g. the frontend's model picker on every
# page load) doesn't hit four providers' APIs on every request.
_REFRESH_TTL = timedelta(minutes=5)

# WHY a short, separate timeout from each adapter's streaming call (120s
# read, for long-running chat turns): a models-list call is a single small
# GET with no reason to ever take that long, and refresh_if_stale() runs
# every provider concurrently — one slow/dead provider must not make every
# models-list request wait 120s.
_MODELS_FETCH_TIMEOUT_SECONDS = 10.0


class ModelEntry:
    """One resolvable model: either fully curated (from catalog.yaml) or
    discovered live with no curated data (capabilities/pricing both None).

    WHY a plain class, not a pydantic BaseModel like CatalogEntry: this type
    is never parsed from external input (JSON or YAML) — it's always built
    in-process from either a CatalogEntry or a ProviderModel — so there's
    nothing for pydantic's validation to do here that a normal __init__
    doesn't already guarantee just as well.
    """

    def __init__(
        self,
        *,
        id: str,
        provider: Provider,
        display_name: str,
        family: str,
        context_window: int | None,
        max_output_tokens: int | None,
        capabilities: Capabilities | None,
        pricing: Pricing | None,
    ) -> None:
        self.id = id
        self.provider = provider
        self.display_name = display_name
        self.family = family
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.capabilities = capabilities
        self.pricing = pricing

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

    @classmethod
    def from_catalog(cls, entry: CatalogEntry) -> "ModelEntry":
        return cls(
            id=entry.id,
            provider=entry.provider,
            display_name=entry.display_name,
            family=entry.family,
            context_window=entry.context_window,
            max_output_tokens=entry.max_output_tokens,
            capabilities=entry.capabilities,
            pricing=entry.pricing,
        )

    @classmethod
    def from_live(
        cls, model: ProviderModel, provider: Provider, catalog_entry: CatalogEntry | None
    ) -> "ModelEntry":
        # WHY the catalog wins outright, not just fills gaps: a curated
        # context_window is a verified number; a live one (when a provider
        # even returns one) is provider-reported and not worth reconciling
        # field-by-field against the curated value.
        if catalog_entry is not None:
            return cls.from_catalog(catalog_entry)
        return cls(
            id=f"{provider}:{model.id}",
            provider=provider,
            display_name=model.id,
            family="unknown",
            context_window=model.context_window,
            max_output_tokens=None,
            capabilities=None,
            pricing=None,
        )


class ModelRegistry:
    """In-memory lookup from a `provider:model` id to its ModelEntry.

    Combines catalog.py's curated data (always present, seeded at
    construction) with each provider's live model list (added/refreshed by
    refresh_if_stale(), best-effort).
    """

    def __init__(self, catalog: dict[str, CatalogEntry]) -> None:
        self._catalog = catalog
        self._entries: dict[str, ModelEntry] = {
            id_: ModelEntry.from_catalog(entry) for id_, entry in catalog.items()
        }
        self._lock = asyncio.Lock()
        self._last_refreshed_at: datetime | None = None

    def resolve(self, model_id: str) -> ModelEntry:
        """Look up a `provider:model` id.

        Raises:
            InvalidRequestError: `model_id` isn't in the registry (§4: "Any
                request naming a model absent from the registry fails fast
                with invalid_request").
        """
        entry = self._entries.get(model_id)
        if entry is None:
            raise InvalidRequestError(
                f"Unknown model {model_id!r}.",
                code="model.not_found",
                details={"model": model_id},
            )
        return entry

    def is_available(self, entry: ModelEntry) -> bool:
        """Whether the entry's provider is currently usable.

        WHY not a stored field: §4 says "available" reflects whether the
        required API key is configured plus the last health probe result,
        which is runtime state, not something authored anywhere. There is
        no health probe yet (GET /api/v1/providers/health, §5.1), so this
        is only the key-configured half of that definition for now.
        """
        return getattr(settings, f"{entry.provider}_api_key") is not None

    def __iter__(self) -> Iterator[ModelEntry]:
        return iter(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)

    async def refresh_if_stale(self) -> None:
        """Re-fetch each configured provider's live model list if the
        current snapshot is older than _REFRESH_TTL.

        Safe to call from multiple concurrent requests — single-flights via
        self._lock rather than piling on duplicate provider calls when the
        cache expires under concurrent traffic.
        """
        if not settings.enable_live_model_refresh:
            return
        if self._is_fresh():
            return
        if self._lock.locked():
            # WHY not await the lock here: a request arriving mid-refresh
            # should serve the current (possibly stale) snapshot immediately
            # rather than block on someone else's in-flight fetch — keeps
            # GET /api/v1/models latency bounded even under this race. The
            # in-flight refresh still lands normally for the *next* request.
            return
        async with self._lock:
            if self._is_fresh():  # someone else may have just finished
                return
            await self._refresh()

    def _is_fresh(self) -> bool:
        return (
            self._last_refreshed_at is not None
            and datetime.now(UTC) - self._last_refreshed_at < _REFRESH_TTL
        )

    async def _refresh(self) -> None:
        configured = [
            (provider, factory(getattr(settings, f"{provider}_api_key")))
            for provider, factory in ADAPTER_CLASSES.items()
            if getattr(settings, f"{provider}_api_key") is not None
        ]
        if not configured:
            self._last_refreshed_at = datetime.now(UTC)
            return

        results = await asyncio.gather(
            *(self._fetch_one(adapter) for _provider, adapter in configured),
            return_exceptions=True,
        )

        # WHY start from the current snapshot, not the catalog alone: a
        # provider whose fetch fails this round must keep whatever live
        # entries it contributed on a previous successful refresh, not lose
        # them — a transient outage should degrade to "stale," not "gone."
        working = dict(self._entries)
        for (provider, _adapter), result in zip(configured, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("model_refresh_failed", provider=provider, error=str(result))
                continue
            live_ids = {f"{provider}:{model.id}" for model in result}
            # Drop live-only entries this provider no longer reports (it
            # retired a model) — but never touch a catalog-backed entry,
            # even if this refresh's live list happens not to include it.
            stale_ids = [
                id_
                for id_, entry in working.items()
                if entry.provider == provider and id_ not in self._catalog and id_ not in live_ids
            ]
            for stale_id in stale_ids:
                del working[stale_id]
            for model in result:
                full_id = f"{provider}:{model.id}"
                working[full_id] = ModelEntry.from_live(model, provider, self._catalog.get(full_id))

        self._entries = working  # atomic swap; concurrent sync readers see old-or-new, never torn
        self._last_refreshed_at = datetime.now(UTC)

    @staticmethod
    async def _fetch_one(adapter: ProviderAdapter) -> list[ProviderModel]:
        async with asyncio.timeout(_MODELS_FETCH_TIMEOUT_SECONDS):
            return await adapter.list_models()


# WHY module-level construction stays synchronous and side-effect-free (no
# network call): importing this module is the startup path, same as before
# this update — ModelRegistry.__init__ only reads the already-loaded
# in-memory CATALOG dict. See the module docstring's first Gotcha for why
# this matters to app/services/chat.py's hot path.
registry = ModelRegistry(CATALOG)
