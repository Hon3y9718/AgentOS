"""POST /api/v1/conversations/{id}/messages, Accept: text/event-stream (API_CONTRACT.md §5.5).

Contract tier for the provider call (ARCHITECTURE.md): respx-mocked
Anthropic transport, no real network — same technique as test_chat.py.
Gotcha: true socket-level client disconnect can't be reliably simulated
through TestClient, so that one scenario calls
app.services.chat.prepare_stream()/emit_stream() directly instead of going
through HTTP — see test_stream_disconnect_persists_incomplete_and_cancelled.
"""

import asyncio
import json
import uuid

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.core.ids import new_id
from app.models.message import Message as MessageModel
from app.schemas.chat import ChatRequest
from app.schemas.content_block import TextBlock
from app.schemas.conversation import ConversationCreate
from app.services import chat as chat_service
from app.services import conversations as conversations_service

BASE = "/api/v1/conversations"
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_MODEL = "anthropic:claude-sonnet-4-5"


@pytest.fixture(autouse=True)
def _configure_anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
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


def _create_conversation(client: TestClient, auth_headers: dict[str, str]) -> str:
    response: dict[str, object] = client.post(
        BASE, json={"default_model": _MODEL}, headers=auth_headers
    ).json()
    return response["id"]  # type: ignore[return-value]


def _chat_headers(
    auth_headers: dict[str, str], idempotency_key: str | None = None
) -> dict[str, str]:
    return {
        **auth_headers,
        "Idempotency-Key": idempotency_key or str(uuid.uuid4()),
        "Accept": "text/event-stream",
    }


def _parse_sse(raw: str) -> list[tuple[str, dict[str, object]]]:
    frames: list[tuple[str, dict[str, object]]] = []
    event_name = ""
    for line in raw.splitlines():
        if line.startswith("event:"):
            event_name = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            frames.append((event_name, json.loads(line.removeprefix("data:").strip())))
    return frames


@respx.mock
def test_stream_happy_path_emits_correct_event_sequence(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    respx.post(_ANTHROPIC_URL).mock(return_value=httpx.Response(200, content=_text_response_body()))
    conversation_id = _create_conversation(client, auth_headers)

    with client.stream(
        "POST",
        f"{BASE}/{conversation_id}/messages",
        json={"content": [{"type": "text", "text": "Hi"}]},
        headers=_chat_headers(auth_headers),
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        raw = "".join(response.iter_text())

    frames = _parse_sse(raw)
    names = [name for name, _ in frames]
    assert names == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert frames[0][1]["run_id"].startswith("run_")  # type: ignore[union-attr]
    assert frames[2][1] == {"index": 0, "delta": {"type": "text_delta", "text": "Hello!"}}
    assert frames[-1][1] == {"status": "complete"}


@respx.mock
def test_stream_tool_call_event_sequence(client: TestClient, auth_headers: dict[str, str]) -> None:
    body = _sse(
        [
            {"type": "message_start", "message": {"usage": {"input_tokens": 20}}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "get_weather"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"city":"NYC"}'},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 12},
            },
            {"type": "message_stop"},
        ]
    )
    respx.post(_ANTHROPIC_URL).mock(return_value=httpx.Response(200, content=body))
    conversation_id = _create_conversation(client, auth_headers)

    with client.stream(
        "POST",
        f"{BASE}/{conversation_id}/messages",
        json={"content": [{"type": "text", "text": "weather?"}]},
        headers=_chat_headers(auth_headers),
    ) as response:
        raw = "".join(response.iter_text())

    frames = _parse_sse(raw)
    start = next(data for name, data in frames if name == "content_block_start")
    assert start["block"] == {"type": "tool_use", "id": "toolu_1", "name": "get_weather"}
    delta = next(data for name, data in frames if name == "content_block_delta")
    assert delta["delta"] == {"type": "input_json_delta", "partial_json": '{"city":"NYC"}'}
    terminal = next(data for name, data in frames if name == "message_delta")
    assert terminal["stop_reason"] == "tool_use"


@respx.mock
def test_stream_persists_final_content_matching_non_streaming_shape(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    respx.post(_ANTHROPIC_URL).mock(return_value=httpx.Response(200, content=_text_response_body()))
    conversation_id = _create_conversation(client, auth_headers)

    with client.stream(
        "POST",
        f"{BASE}/{conversation_id}/messages",
        json={"content": [{"type": "text", "text": "Hi"}]},
        headers=_chat_headers(auth_headers),
    ) as response:
        "".join(response.iter_text())  # drain the stream to completion

    messages = client.get(f"{BASE}/{conversation_id}/messages", headers=auth_headers).json()
    assistant = next(m for m in messages["data"] if m["role"] == "assistant")
    assert assistant["status"] == "complete"
    assert assistant["content"] == [{"type": "text", "text": "Hello!"}]
    assert assistant["usage"]["cost_usd"] == "0.000105"


@respx.mock
def test_stream_mid_stream_error_emits_error_then_message_stop_failed(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    body = _sse(
        [
            {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "partial"},
            },
            {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
        ]
    )
    respx.post(_ANTHROPIC_URL).mock(return_value=httpx.Response(200, content=body))
    conversation_id = _create_conversation(client, auth_headers)

    with client.stream(
        "POST",
        f"{BASE}/{conversation_id}/messages",
        json={"content": [{"type": "text", "text": "Hi"}]},
        headers=_chat_headers(auth_headers),
    ) as response:
        # WHY 200, not 503: §5.5 — "HTTP status is already 200 at that point
        # and cannot be changed... clients must handle in-stream errors."
        assert response.status_code == 200
        raw = "".join(response.iter_text())

    frames = _parse_sse(raw)
    names = [name for name, _ in frames]
    assert names[-2:] == ["error", "message_stop"]
    assert frames[-2][1]["error"]["type"] == "provider_unavailable"  # type: ignore[index]
    assert frames[-1][1] == {"status": "failed"}

    messages = client.get(f"{BASE}/{conversation_id}/messages", headers=auth_headers).json()
    assistant = next(m for m in messages["data"] if m["role"] == "assistant")
    assert assistant["status"] == "failed"
    assert assistant["content"] == [{"type": "text", "text": "partial"}]


@respx.mock
def test_stream_idempotency_replay_reconstructs_sse(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    route = respx.post(_ANTHROPIC_URL).mock(
        return_value=httpx.Response(200, content=_text_response_body())
    )
    conversation_id = _create_conversation(client, auth_headers)
    headers = _chat_headers(auth_headers, str(uuid.uuid4()))
    body = {"content": [{"type": "text", "text": "Hi"}]}

    with client.stream(
        "POST", f"{BASE}/{conversation_id}/messages", json=body, headers=headers
    ) as r1:
        frames1 = _parse_sse("".join(r1.iter_text()))
    with client.stream(
        "POST", f"{BASE}/{conversation_id}/messages", json=body, headers=headers
    ) as r2:
        frames2 = _parse_sse("".join(r2.iter_text()))

    # WHY this is the assertion that matters, same as test_chat.py's
    # non-streaming version: idempotency exists to stop a retry from
    # duplicating the actual turn.
    assert route.call_count == 1

    names1 = [name for name, _ in frames1]
    names2 = [name for name, _ in frames2]
    expected = [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert names1 == expected
    assert names2 == expected
    # WHY not asserting frames1 == frames2: replay reconstructs a
    # single-shot sequence from persisted final state, not the original
    # chunking, and each message_start carries a freshly generated run_id
    # (see emit_stream's docstring) — only the conveyed content must match.
    assert frames1[2][1]["delta"]["text"] == frames2[2][1]["delta"]["text"] == "Hello!"  # type: ignore[index]


@respx.mock
def test_stream_emits_ping_during_provider_silence(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.chat.PING_INTERVAL_SECONDS", 0.05)

    async def _slow_response(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(200, content=_text_response_body())

    respx.post(_ANTHROPIC_URL).mock(side_effect=_slow_response)
    conversation_id = _create_conversation(client, auth_headers)

    with client.stream(
        "POST",
        f"{BASE}/{conversation_id}/messages",
        json={"content": [{"type": "text", "text": "Hi"}]},
        headers=_chat_headers(auth_headers),
    ) as response:
        raw = "".join(response.iter_text())

    names = [name for name, _ in _parse_sse(raw)]
    assert "ping" in names


async def test_stream_disconnect_persists_incomplete_and_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct generator-level test — see module docstring on why this one
    scenario bypasses HTTP/TestClient entirely."""
    monkeypatch.setattr("app.core.llm.registry.settings.anthropic_api_key", "sk-test")
    body = _text_response_body(text="will be cut off")

    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db:
        conversation = await conversations_service.create_conversation(
            db, "disconnect_test_user", ConversationCreate(default_model=_MODEL)
        )

        with respx.mock:
            respx.post(_ANTHROPIC_URL).mock(return_value=httpx.Response(200, content=body))

            data = ChatRequest(content=[TextBlock(text="Hi")])
            plan = await chat_service.prepare_stream(
                db, "disconnect_test_user", conversation.id, new_id("idem"), data
            )

            call_count = 0

            async def fake_is_disconnected() -> bool:
                nonlocal call_count
                call_count += 1
                # WHY > 3: lets content_block_start/delta/stop (3 loop
                # iterations) through first, so there's real partial content
                # to assert on, then disconnects before message_delta.
                return call_count > 3

            frames = [
                frame
                async for frame in chat_service.emit_stream(
                    db,
                    plan,
                    request_id="req_test",
                    is_disconnected=fake_is_disconnected,
                )
            ]

        # WHY no message_stop: the client is gone by the time the stream
        # would have finished — there's no connection left to send it on.
        assert all(not frame.startswith("event: message_stop") for frame in frames)

        stmt = select(MessageModel).where(
            MessageModel.conversation_id == conversation.id, MessageModel.role == "assistant"
        )
        assistant = (await db.execute(stmt)).scalar_one()
        assert assistant.status == "incomplete"
        assert assistant.stop_reason == "cancelled"
        assert assistant.content == [{"type": "text", "text": "will be cut off"}]

    await engine.dispose()
