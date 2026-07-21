"""app/core/llm/groq_adapter.py (API_CONTRACT.md §5.5, ADR-0002).

Contract tier (ARCHITECTURE.md): fixture-driven, mocked transport, no real
network. Live-verified by hand against the real Groq API during
implementation — full happy path (text and tool-call) both confirmed
working end-to-end, including the usage-field location (see the adapter's
own module docstring) and error envelope shape (see BUILD_LOG.md).
"""

import json

import httpx
import pytest
import respx

from app.core.errors import (
    InternalError,
    InvalidRequestError,
    ProviderUnavailableError,
    RateLimitedError,
)
from app.core.llm.groq_adapter import GroqAdapter
from app.core.llm.types import (
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    InputJsonDelta,
    LLMMessage,
    LLMParams,
    LLMRequest,
    MessageDelta,
    TextBlockStart,
    TextDelta,
    ToolDefinition,
    ToolUseBlockStart,
)
from app.schemas.content_block import TextBlock

_URL = "https://api.groq.com/openai/v1/chat/completions"
_MODELS_URL = "https://api.groq.com/openai/v1/models"


def _sse(chunks: list[dict[str, object]]) -> bytes:
    frames = [f"data: {json.dumps(c)}\n\n" for c in chunks]
    frames.append("data: [DONE]\n\n")
    return "".join(frames).encode()


def _request(**overrides: object) -> LLMRequest:
    defaults: dict[str, object] = {
        "model": "llama-3.3-70b-versatile",
        "system_prompt": None,
        "messages": [LLMMessage(role="user", content=[TextBlock(text="hi")])],
        "params": LLMParams(max_tokens=1024),
    }
    defaults.update(overrides)
    return LLMRequest(**defaults)  # type: ignore[arg-type]


@respx.mock
async def test_stream_translates_text_response() -> None:
    # WHY this exact shape: matches what the real API returned when checked
    # live during implementation (queue_time/prompt_time fields included,
    # same as Groq's actual usage payload — extra fields we don't read).
    body = _sse(
        [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"content": "Hel"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {
                "choices": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "queue_time": 0.01},
            },
        ]
    )
    respx.post(_URL).mock(return_value=httpx.Response(200, content=body))

    adapter = GroqAdapter(api_key="gsk-test")
    events = [event async for event in adapter.stream(_request())]

    assert events[0] == ContentBlockStart(index=0, block=TextBlockStart())
    assert events[1] == ContentBlockDelta(index=0, delta=TextDelta(text="Hel"))
    assert events[2] == ContentBlockDelta(index=0, delta=TextDelta(text="lo"))
    assert events[3] == ContentBlockStop(index=0)
    terminal = events[4]
    assert isinstance(terminal, MessageDelta)
    assert terminal.stop_reason == "end_turn"
    assert terminal.usage.input_tokens == 10
    assert terminal.usage.output_tokens == 5


@respx.mock
async def test_stream_translates_tool_call_response() -> None:
    body = _sse(
        [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_weather",
                                        "arguments": '{"city":"Paris"}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 12}},
        ]
    )
    respx.post(_URL).mock(return_value=httpx.Response(200, content=body))

    adapter = GroqAdapter(api_key="gsk-test")
    request = _request(
        tools=[
            ToolDefinition(
                name="get_weather", description="Get the weather", input_schema={"type": "object"}
            )
        ]
    )
    events = [event async for event in adapter.stream(request)]

    start = events[0]
    assert isinstance(start, ContentBlockStart)
    assert isinstance(start.block, ToolUseBlockStart)
    assert start.block.id == "call_1"
    assert start.block.name == "get_weather"

    delta = events[1]
    assert isinstance(delta, ContentBlockDelta)
    assert isinstance(delta.delta, InputJsonDelta)
    assert delta.delta.partial_json == '{"city":"Paris"}'

    terminal = events[-1]
    assert isinstance(terminal, MessageDelta)
    assert terminal.stop_reason == "tool_use"


@respx.mock
async def test_stream_maps_length_to_max_tokens_stop_reason() -> None:
    body = _sse(
        [
            {"choices": [{"index": 0, "delta": {"content": "cut off"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]},
            {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 1024}},
        ]
    )
    respx.post(_URL).mock(return_value=httpx.Response(200, content=body))

    adapter = GroqAdapter(api_key="gsk-test")
    events = [event async for event in adapter.stream(_request())]

    terminal = events[-1]
    assert isinstance(terminal, MessageDelta)
    assert terminal.stop_reason == "max_tokens"


@respx.mock
async def test_stream_maps_pre_stream_rate_limit_error() -> None:
    respx.post(_URL).mock(
        return_value=httpx.Response(
            429,
            json={"error": {"type": "rate_limit_error", "message": "slow down", "code": None}},
            headers={"retry-after": "30"},
        )
    )

    adapter = GroqAdapter(api_key="gsk-test")
    with pytest.raises(RateLimitedError) as exc_info:
        async for _ in adapter.stream(_request()):
            pass

    assert exc_info.value.retry_after_seconds == 30


@respx.mock
async def test_stream_maps_model_not_found_to_invalid_request() -> None:
    # WHY this exact shape: confirmed live against the real API during
    # implementation — Groq returns HTTP 404 (not 400) for an unknown
    # model, with this exact error envelope.
    respx.post(_URL).mock(
        return_value=httpx.Response(
            404,
            json={
                "error": {
                    "message": "The model `not-a-real-model` does not exist.",
                    "type": "invalid_request_error",
                    "code": "model_not_found",
                }
            },
        )
    )

    adapter = GroqAdapter(api_key="gsk-test")
    with pytest.raises(InvalidRequestError):
        async for _ in adapter.stream(_request()):
            pass


@respx.mock
async def test_stream_maps_insufficient_quota_to_provider_unavailable() -> None:
    respx.post(_URL).mock(
        return_value=httpx.Response(
            429,
            json={
                "error": {
                    "type": "insufficient_quota",
                    "message": "You exceeded your current quota.",
                    "code": "insufficient_quota",
                }
            },
        )
    )

    adapter = GroqAdapter(api_key="gsk-test")
    with pytest.raises(ProviderUnavailableError):
        async for _ in adapter.stream(_request()):
            pass


@respx.mock
async def test_list_models_includes_context_window() -> None:
    # WHY context_window is asserted non-None here, unlike
    # OpenAI/Anthropic's equivalent tests: verified live during
    # implementation — Groq's /openai/v1/models response DOES report it
    # per model (15 real entries as of this check), unlike OpenAI's own
    # endpoint.
    respx.get(_MODELS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {
                        "id": "llama-3.3-70b-versatile",
                        "object": "model",
                        "active": True,
                        "context_window": 131072,
                    }
                ],
            },
        )
    )

    adapter = GroqAdapter(api_key="gsk-test")
    models = await adapter.list_models()

    assert models[0].id == "llama-3.3-70b-versatile"
    assert models[0].context_window == 131072


@respx.mock
async def test_list_models_maps_error() -> None:
    respx.get(_MODELS_URL).mock(
        return_value=httpx.Response(
            401,
            json={"error": {"type": "authentication_error", "message": "bad key"}},
        )
    )

    adapter = GroqAdapter(api_key="gsk-bad")
    with pytest.raises(InternalError) as exc_info:
        await adapter.list_models()
    assert exc_info.value.details["provider"] == "groq"
