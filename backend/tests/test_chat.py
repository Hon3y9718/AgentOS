"""POST /api/v1/conversations/{id}/messages — the chat endpoint (API_CONTRACT.md §5.4).

Non-streaming only (Accept: application/json) — §5.5 (SSE) isn't built yet.
Contract tier for the provider call (ARCHITECTURE.md): respx-mocked
Anthropic transport, no real network — same technique as
test_anthropic_adapter.py, now exercised through the full ASGI app.
"""

import asyncio
import json
import uuid

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.schemas.conversation import Conversation, ConversationCreate
from app.services import conversations as conversations_service

BASE = "/api/v1/conversations"
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_MODEL = "anthropic:claude-sonnet-4-5"


@pytest.fixture(autouse=True)
def _configure_anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # WHY needed: this environment has no real ANTHROPIC_API_KEY configured
    # (see docs/BUILD_LOG.md's core/llm session) — without this,
    # registry.is_available() is False and every test would fail before
    # ever reaching the (mocked) provider call.
    monkeypatch.setattr("app.core.llm.registry.settings.anthropic_api_key", "sk-test")


def _sse(events: list[dict[str, object]]) -> bytes:
    frames = [f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events]
    return "".join(frames).encode()


def _text_response_body(
    text: str = "Hello!", *, input_tokens: int = 10, output_tokens: int = 5
) -> bytes:
    return _sse(
        [
            {"type": "message_start", "message": {"usage": {"input_tokens": input_tokens}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": output_tokens},
            },
            {"type": "message_stop"},
        ]
    )


def _create_conversation(
    client: TestClient, auth_headers: dict[str, str], *, default_model: str | None = _MODEL
) -> str:
    response: dict[str, object] = client.post(
        BASE, json={"default_model": default_model}, headers=auth_headers
    ).json()
    return response["id"]  # type: ignore[return-value]


def _create_conversation_for_user(user_id: str) -> Conversation:
    async def _run() -> Conversation:
        engine = create_async_engine(settings.database_url)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as db:
            result = await conversations_service.create_conversation(
                db, user_id, ConversationCreate(default_model=_MODEL)
            )
        await engine.dispose()
        return result

    return asyncio.run(_run())


def _chat_headers(
    auth_headers: dict[str, str], idempotency_key: str | None = None
) -> dict[str, str]:
    # WHY a fresh UUID by default, not a fixed literal: this repo has no
    # per-test DB isolation (tests share one Postgres, per BUILD_LOG) and
    # Idempotency-Key rows are looked up by key alone — a shared default
    # literal across tests would make a later test silently replay an
    # earlier test's cached response instead of exercising its own
    # scenario. Tests that specifically need a *repeated* key (the
    # idempotency-behavior tests themselves) pass one explicitly.
    return {**auth_headers, "Idempotency-Key": idempotency_key or str(uuid.uuid4())}


@respx.mock
def test_chat_happy_path_returns_201_with_user_and_assistant_messages(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    respx.post(_ANTHROPIC_URL).mock(return_value=httpx.Response(200, content=_text_response_body()))
    conversation_id = _create_conversation(client, auth_headers)

    response = client.post(
        f"{BASE}/{conversation_id}/messages",
        json={"content": [{"type": "text", "text": "Hi"}]},
        headers=_chat_headers(auth_headers),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["user_message"]["role"] == "user"
    assert body["user_message"]["content"] == [{"type": "text", "text": "Hi"}]
    assert body["assistant_message"]["role"] == "assistant"
    assert body["assistant_message"]["status"] == "complete"
    assert body["assistant_message"]["content"] == [{"type": "text", "text": "Hello!"}]
    assert body["assistant_message"]["stop_reason"] == "end_turn"
    usage = body["assistant_message"]["usage"]
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 5
    assert usage["cost_usd"] == "0.000105"


def test_chat_missing_idempotency_key_is_422(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    conversation_id = _create_conversation(client, auth_headers)

    response = client.post(
        f"{BASE}/{conversation_id}/messages",
        json={"content": [{"type": "text", "text": "Hi"}]},
        headers=auth_headers,
    )

    assert response.status_code == 422


@respx.mock
def test_chat_idempotency_replay_same_body_returns_original_result(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    route = respx.post(_ANTHROPIC_URL).mock(
        return_value=httpx.Response(200, content=_text_response_body())
    )
    conversation_id = _create_conversation(client, auth_headers)
    body = {"content": [{"type": "text", "text": "Hi"}]}
    # WHY a freshly generated key, not a fixed literal: the DB persists
    # across separate pytest invocations (no per-test isolation, shared dev
    # Postgres) — a fixed literal here would replay a *previous run's*
    # cached response on the very first call, never touching the mock.
    headers = _chat_headers(auth_headers, str(uuid.uuid4()))

    first = client.post(f"{BASE}/{conversation_id}/messages", json=body, headers=headers)
    second = client.post(f"{BASE}/{conversation_id}/messages", json=body, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()
    # WHY this is the assertion that matters: §5.4 — idempotency exists to
    # stop a retry from duplicating the actual turn, not just to return a
    # matching-looking response.
    assert route.call_count == 1


@respx.mock
def test_chat_idempotency_replay_different_body_is_409(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    respx.post(_ANTHROPIC_URL).mock(return_value=httpx.Response(200, content=_text_response_body()))
    conversation_id = _create_conversation(client, auth_headers)
    headers = _chat_headers(auth_headers, str(uuid.uuid4()))

    first = client.post(
        f"{BASE}/{conversation_id}/messages",
        json={"content": [{"type": "text", "text": "Hi"}]},
        headers=headers,
    )
    second = client.post(
        f"{BASE}/{conversation_id}/messages",
        json={"content": [{"type": "text", "text": "Something else"}]},
        headers=headers,
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["type"] == "conflict"


@respx.mock
def test_chat_model_falls_back_to_conversation_default(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    respx.post(_ANTHROPIC_URL).mock(return_value=httpx.Response(200, content=_text_response_body()))
    conversation_id = _create_conversation(client, auth_headers, default_model=_MODEL)

    response = client.post(
        f"{BASE}/{conversation_id}/messages",
        json={"content": [{"type": "text", "text": "Hi"}]},
        headers=_chat_headers(auth_headers),
    )

    assert response.status_code == 201
    assert response.json()["assistant_message"]["model"] == _MODEL


def test_chat_missing_model_everywhere_is_invalid_request(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    conversation_id = _create_conversation(client, auth_headers, default_model=None)

    response = client.post(
        f"{BASE}/{conversation_id}/messages",
        json={"content": [{"type": "text", "text": "Hi"}]},
        headers=_chat_headers(auth_headers),
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request"


def test_chat_unknown_model_is_invalid_request(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    conversation_id = _create_conversation(client, auth_headers, default_model=None)

    response = client.post(
        f"{BASE}/{conversation_id}/messages",
        json={"content": [{"type": "text", "text": "Hi"}], "model": "openai:does-not-exist"},
        headers=_chat_headers(auth_headers),
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request"


@respx.mock
def test_chat_provider_error_persists_failed_assistant_message_with_partial_content(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    body = _sse(
        [
            {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "partial answer"},
            },
            {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
        ]
    )
    respx.post(_ANTHROPIC_URL).mock(return_value=httpx.Response(200, content=body))
    conversation_id = _create_conversation(client, auth_headers)

    response = client.post(
        f"{BASE}/{conversation_id}/messages",
        json={"content": [{"type": "text", "text": "Hi"}]},
        headers=_chat_headers(auth_headers),
    )

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "provider_unavailable"

    messages = client.get(f"{BASE}/{conversation_id}/messages", headers=auth_headers).json()
    assistant_messages = [m for m in messages["data"] if m["role"] == "assistant"]
    assert len(assistant_messages) == 1
    assert assistant_messages[0]["status"] == "failed"
    assert assistant_messages[0]["content"] == [{"type": "text", "text": "partial answer"}]


def test_chat_unsupported_tools_field_is_invalid_request(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    conversation_id = _create_conversation(client, auth_headers)

    response = client.post(
        f"{BASE}/{conversation_id}/messages",
        json={"content": [{"type": "text", "text": "Hi"}], "tools": ["get_weather"]},
        headers=_chat_headers(auth_headers),
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request"


def test_chat_nonexistent_conversation_is_404(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        f"{BASE}/conv_doesnotexist/messages",
        json={"content": [{"type": "text", "text": "Hi"}]},
        headers=_chat_headers(auth_headers),
    )

    assert response.status_code == 404


def test_chat_other_users_conversation_is_404(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    other = _create_conversation_for_user("some_other_user")

    response = client.post(
        f"{BASE}/{other.id}/messages",
        json={"content": [{"type": "text", "text": "Hi"}]},
        headers=_chat_headers(auth_headers),
    )

    assert response.status_code == 404


def test_chat_without_auth_is_401(client: TestClient) -> None:
    response = client.post(
        f"{BASE}/conv_doesnotexist/messages",
        json={"content": [{"type": "text", "text": "Hi"}]},
        headers={"Idempotency-Key": "idem-noauth"},
    )

    assert response.status_code == 401


@respx.mock
def test_chat_bumps_message_count_and_updated_at(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    respx.post(_ANTHROPIC_URL).mock(return_value=httpx.Response(200, content=_text_response_body()))
    conversation_id = _create_conversation(client, auth_headers)
    before = client.get(f"{BASE}/{conversation_id}", headers=auth_headers).json()
    assert before["message_count"] == 0

    client.post(
        f"{BASE}/{conversation_id}/messages",
        json={"content": [{"type": "text", "text": "Hi"}]},
        headers=_chat_headers(auth_headers),
    )

    after = client.get(f"{BASE}/{conversation_id}", headers=auth_headers).json()
    assert after["message_count"] == 2
    assert after["updated_at"] > before["updated_at"]


@respx.mock
def test_chat_persists_null_cost_usd_for_model_with_no_curated_pricing(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-07-21 live-discovery update: a model resolvable in the registry
    but absent from catalog.yaml has entry.pricing=None — the turn must
    still complete and persist usage.cost_usd as null, not raise or
    fabricate a price. See core/llm/registry.py's ModelEntry and
    chat.py's _usage_dict()."""
    from app.core.llm.registry import ModelEntry, registry

    unpriced_id = "anthropic:claude-live-only-test"
    unpriced_entry = ModelEntry(
        id=unpriced_id,
        provider="anthropic",
        display_name="claude-live-only-test",
        family="unknown",
        context_window=None,
        max_output_tokens=None,
        capabilities=None,
        pricing=None,
    )
    # WHY setitem on the shared singleton's private _entries, not a fresh
    # ModelRegistry: chat.py's service functions import `registry` directly
    # from app.core.llm.registry at module scope — there's no seam to swap
    # in a different instance for one request without also patching every
    # one of those import sites. monkeypatch restores this automatically.
    monkeypatch.setitem(registry._entries, unpriced_id, unpriced_entry)  # type: ignore[attr-defined]

    respx.post(_ANTHROPIC_URL).mock(return_value=httpx.Response(200, content=_text_response_body()))
    conversation_id = _create_conversation(client, auth_headers, default_model=unpriced_id)

    response = client.post(
        f"{BASE}/{conversation_id}/messages",
        json={"content": [{"type": "text", "text": "Hi"}]},
        headers=_chat_headers(auth_headers),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["assistant_message"]["status"] == "complete"
    assert body["assistant_message"]["usage"]["cost_usd"] is None
