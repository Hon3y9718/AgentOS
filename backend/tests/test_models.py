"""GET /api/v1/models (API_CONTRACT.md §4).

Integration tier (ARCHITECTURE.md): through the real ASGI app. No DB
involved — but the endpoint still goes through the same TestClient +
auth_headers fixtures as every other router for consistency, and because
auth is still required (see models.py's own gotcha comment).
ENABLE_LIVE_MODEL_REFRESH is forced false in tests/conftest.py, so every
test here exercises the catalog-seeded state only — the module-level
`registry` singleton never grows beyond catalog.yaml's 4 entries during a
test run. Live-merge behavior itself (a model with no catalog match) is
covered in test_registry.py and, at the schema-filtering level, by the
_live_only_entry()-based tests below.
"""

import pytest
from fastapi.testclient import TestClient

from app.core.llm.registry import ModelEntry, registry
from app.services.models import _matches, _to_schema

BASE = "/api/v1/models"


def _live_only_entry() -> ModelEntry:
    """A model discovered live but absent from catalog.yaml — the shape
    GET /api/v1/models must still serve without crashing (§4, 2026-07-21
    live-discovery update)."""
    return ModelEntry(
        id="openai:some-new-model",
        provider="openai",
        display_name="some-new-model",
        family="unknown",
        context_window=None,
        max_output_tokens=None,
        capabilities=None,
        pricing=None,
    )


def test_list_models_returns_every_registry_entry(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.get(BASE, headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert len(body["data"]) == len(registry)
    ids = {m["id"] for m in body["data"]}
    assert "anthropic:claude-sonnet-4-5" in ids
    assert "openai:gpt-4o" in ids
    assert "groq:llama-3.3-70b-versatile" in ids
    assert "together:meta-llama/Llama-3.3-70B-Instruct-Turbo" in ids


def test_list_models_requires_auth(client: TestClient) -> None:
    response = client.get(BASE)

    assert response.status_code == 401


def test_list_models_shape_matches_contract_example(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.get(BASE, headers=auth_headers)

    entry = next(m for m in response.json()["data"] if m["id"] == "anthropic:claude-sonnet-4-5")
    assert entry["provider"] == "anthropic"
    assert entry["display_name"] == "Claude Sonnet 4.5"
    assert entry["family"] == "claude"
    assert entry["context_window"] == 200000
    assert entry["max_output_tokens"] == 64000
    assert entry["capabilities"]["streaming"] is True
    assert entry["pricing"]["input_per_mtok_usd"] == "3.00"
    assert entry["deprecated_at"] is None


def test_list_models_filters_by_provider(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.get(BASE, params={"provider": "groq"}, headers=auth_headers)

    body = response.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["id"] == "groq:llama-3.3-70b-versatile"


def test_list_models_unknown_provider_returns_empty(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.get(BASE, params={"provider": "not-a-real-provider"}, headers=auth_headers)

    assert response.json()["data"] == []


def test_list_models_filters_by_single_capability(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.get(BASE, params={"capability": "vision"}, headers=auth_headers)

    ids = {m["id"] for m in response.json()["data"]}
    # WHY these two specifically: catalog.yaml sets vision=true only on
    # anthropic:claude-sonnet-4-5 and openai:gpt-4o; groq/together's llama
    # entries are both vision=false.
    assert ids == {"anthropic:claude-sonnet-4-5", "openai:gpt-4o"}


def test_list_models_ands_multiple_capabilities(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.get(
        BASE,
        params=[("capability", "vision"), ("capability", "reasoning")],
        headers=auth_headers,
    )

    # WHY exactly one entry, not two: openai:gpt-4o has vision=true but
    # reasoning=false (see catalog.yaml's own WHY comment on that field) —
    # only the anthropic entry has both, which is what ANDing must produce.
    ids = {m["id"] for m in response.json()["data"]}
    assert ids == {"anthropic:claude-sonnet-4-5"}


def test_list_models_unknown_capability_returns_empty(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.get(
        BASE, params={"capability": "not-a-real-capability"}, headers=auth_headers
    )

    assert response.json()["data"] == []


def test_list_models_filters_by_available(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.core.llm.registry.settings.anthropic_api_key", None)
    monkeypatch.setattr("app.core.llm.registry.settings.openai_api_key", None)
    monkeypatch.setattr("app.core.llm.registry.settings.groq_api_key", None)
    monkeypatch.setattr("app.core.llm.registry.settings.together_api_key", "together-test")

    response = client.get(BASE, params={"available": "true"}, headers=auth_headers)

    body = response.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["id"] == "together:meta-llama/Llama-3.3-70B-Instruct-Turbo"
    assert body["data"][0]["available"] is True


def test_matches_excludes_live_only_entry_from_capability_filter() -> None:
    # WHY a unit test against _matches()/_to_schema() directly, not through
    # the HTTP endpoint like every test above: exercising this via a real
    # GET /api/v1/models call would require mutating the shared,
    # process-wide `registry` singleton with a fake live-discovered entry —
    # test pollution other tests would then see. A hand-built ModelEntry
    # avoids that entirely.
    live_only = _live_only_entry()

    # getattr(None, name, False) is what makes this work — no crash, no
    # special-case needed for entry.capabilities being None (see
    # models.py's own comment on _matches()).
    assert _matches(live_only, provider=None, capabilities=["vision"], available=None) is False
    assert _matches(live_only, provider=None, capabilities=[], available=None) is True


def test_to_schema_passes_through_null_capabilities_and_pricing() -> None:
    schema = _to_schema(_live_only_entry())

    assert schema.capabilities is None
    assert schema.pricing is None
    assert schema.context_window is None
    assert schema.max_output_tokens is None
