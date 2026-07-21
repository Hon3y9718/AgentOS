"""app/core/llm/registry.py — live model registry (API_CONTRACT.md §4)."""

import asyncio

import pytest

from app.core.errors import InvalidRequestError
from app.core.llm.catalog import CATALOG
from app.core.llm.registry import ModelRegistry, registry
from app.core.llm.types import ProviderModel


def test_registry_loads_at_least_one_model() -> None:
    assert len(registry) > 0


def test_resolve_known_model_returns_entry() -> None:
    entry = registry.resolve("anthropic:claude-sonnet-4-5")

    assert entry.provider == "anthropic"
    assert entry.capabilities is not None
    assert entry.capabilities.streaming is True
    assert entry.pricing is not None
    assert entry.pricing.input_per_mtok_usd == "3.00"


def test_resolve_unknown_model_raises_invalid_request() -> None:
    with pytest.raises(InvalidRequestError) as exc_info:
        registry.resolve("openai:does-not-exist")

    assert exc_info.value.type == "invalid_request"
    assert exc_info.value.details == {"model": "openai:does-not-exist"}


def test_is_available_reflects_configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = registry.resolve("anthropic:claude-sonnet-4-5")

    monkeypatch.setattr("app.core.llm.registry.settings.anthropic_api_key", "sk-test")
    assert registry.is_available(entry) is True

    monkeypatch.setattr("app.core.llm.registry.settings.anthropic_api_key", None)
    assert registry.is_available(entry) is False


class _FakeAdapter:
    """A ProviderAdapter stand-in whose list_models() is fully controlled by
    the test — no httpx, no respx involved here; these tests are about
    registry.py's own merge/cache logic, not any real adapter's wire
    translation (that's each adapter's own test file's job)."""

    def __init__(self, models: list[ProviderModel] | None = None) -> None:
        self.models = models or []
        self.fails = False
        self.call_count = 0

    async def list_models(self) -> list[ProviderModel]:
        self.call_count += 1
        if self.fails:
            raise RuntimeError("simulated provider outage")
        return self.models


# WHY every test below builds its own `ModelRegistry(CATALOG)`, never
# mutating the shared module-level `registry`: refresh_if_stale() mutates
# internal state (_entries, _last_refreshed_at) — sharing one instance
# across tests would make them order-dependent.


async def test_refresh_adds_live_only_entry_with_null_capabilities_and_pricing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = ModelRegistry(CATALOG)
    fake = _FakeAdapter([ProviderModel(id="gpt-5-new", context_window=200000)])
    monkeypatch.setattr("app.core.llm.registry.settings.enable_live_model_refresh", True)
    monkeypatch.setattr("app.core.llm.registry.settings.openai_api_key", "sk-test")
    monkeypatch.setattr("app.core.llm.registry.ADAPTER_CLASSES", {"openai": lambda api_key: fake})

    await fresh.refresh_if_stale()
    entry = fresh.resolve("openai:gpt-5-new")

    assert entry.capabilities is None
    assert entry.pricing is None
    assert entry.context_window == 200000
    # A refresh that only adds a new live model must not touch existing
    # catalog-backed entries.
    catalog_entry = fresh.resolve("anthropic:claude-sonnet-4-5")
    assert catalog_entry.capabilities is not None


async def test_refresh_keeps_prior_entries_when_provider_then_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = ModelRegistry(CATALOG)
    fake = _FakeAdapter([ProviderModel(id="gpt-5-new")])
    monkeypatch.setattr("app.core.llm.registry.settings.enable_live_model_refresh", True)
    monkeypatch.setattr("app.core.llm.registry.settings.openai_api_key", "sk-test")
    monkeypatch.setattr("app.core.llm.registry.ADAPTER_CLASSES", {"openai": lambda api_key: fake})

    await fresh.refresh_if_stale()
    assert fresh.resolve("openai:gpt-5-new").id == "openai:gpt-5-new"

    # WHY reach into _last_refreshed_at directly: this is a white-box test
    # of the TTL-bypass path specifically — forcing staleness without
    # waiting out the real 5-minute TTL.
    fresh._last_refreshed_at = None
    fake.fails = True
    await fresh.refresh_if_stale()

    # The entry from the successful first refresh must survive a later
    # failed one — a transient outage degrades to "stale," not "gone."
    assert fresh.resolve("openai:gpt-5-new").id == "openai:gpt-5-new"


async def test_refresh_is_single_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    fresh = ModelRegistry(CATALOG)
    fake = _FakeAdapter([ProviderModel(id="gpt-5-new")])
    monkeypatch.setattr("app.core.llm.registry.settings.enable_live_model_refresh", True)
    monkeypatch.setattr("app.core.llm.registry.settings.openai_api_key", "sk-test")
    monkeypatch.setattr("app.core.llm.registry.ADAPTER_CLASSES", {"openai": lambda api_key: fake})

    await asyncio.gather(
        fresh.refresh_if_stale(), fresh.refresh_if_stale(), fresh.refresh_if_stale()
    )

    assert fake.call_count == 1


async def test_refresh_if_stale_is_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # WHY this test exists: a canary for tests/conftest.py's
    # ENABLE_LIVE_MODEL_REFRESH=false setdefault. If that ever gets
    # removed, this is the test that should start failing loudly rather
    # than `make test` silently starting to make real provider calls.
    fresh = ModelRegistry(CATALOG)
    fake = _FakeAdapter([ProviderModel(id="gpt-5-new")])
    monkeypatch.setattr("app.core.llm.registry.settings.enable_live_model_refresh", False)
    monkeypatch.setattr("app.core.llm.registry.settings.openai_api_key", "sk-test")
    monkeypatch.setattr("app.core.llm.registry.ADAPTER_CLASSES", {"openai": lambda api_key: fake})

    await fresh.refresh_if_stale()

    assert fake.call_count == 0
