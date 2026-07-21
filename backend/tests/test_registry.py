"""app/core/llm/registry.py — model registry loader (API_CONTRACT.md §4)."""

import pytest

from app.core.errors import InvalidRequestError
from app.core.llm.registry import registry


def test_registry_loads_at_least_one_model() -> None:
    assert len(registry) > 0


def test_resolve_known_model_returns_entry() -> None:
    entry = registry.resolve("anthropic:claude-sonnet-4-5")

    assert entry.provider == "anthropic"
    assert entry.capabilities.streaming is True
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
