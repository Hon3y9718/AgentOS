"""app/core/llm/openai_adapter.py (API_CONTRACT.md §5.5, ADR-0002).

Contract tier (ARCHITECTURE.md): fixture-driven, mocked transport, no real
network. Live-verified once by hand against the real OpenAI API during
implementation — confirmed auth headers are accepted and error mapping
works correctly (the configured key has no billing/quota, so the happy path
itself couldn't be exercised live — see docs/BUILD_LOG.md).
"""

import json

import httpx
import pytest
import respx

from app.core.errors import (
    ContextLengthExceededError,
    InternalError,
    ProviderUnavailableError,
    RateLimitedError,
)
from app.core.llm.openai_adapter import OpenAIAdapter
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

_URL = "https://api.openai.com/v1/chat/completions"
_MODELS_URL = "https://api.openai.com/v1/models"


def _sse(chunks: list[dict[str, object]]) -> bytes:
    # WHY no "event:" line, unlike anthropic_adapter's fixtures: OpenAI's
    # stream carries only "data:" lines — the chunk JSON itself has no
    # discriminator field this adapter dispatches on (it dispatches on
    # choices[0].delta's shape instead).
    frames = [f"data: {json.dumps(c)}\n\n" for c in chunks]
    frames.append("data: [DONE]\n\n")
    return "".join(frames).encode()


def _request(**overrides: object) -> LLMRequest:
    defaults: dict[str, object] = {
        "model": "gpt-4o",
        "system_prompt": None,
        "messages": [LLMMessage(role="user", content=[TextBlock(text="hi")])],
        "params": LLMParams(max_tokens=1024),
    }
    defaults.update(overrides)
    return LLMRequest(**defaults)  # type: ignore[arg-type]


@respx.mock
async def test_stream_translates_text_response() -> None:
    body = _sse(
        [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"content": "Hel"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        ]
    )
    respx.post(_URL).mock(return_value=httpx.Response(200, content=body))

    adapter = OpenAIAdapter(api_key="sk-test")
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
    # WHY id/name only on the first chunk for a given tool_calls[].index:
    # matches OpenAI's real streaming behavior — subsequent chunks for the
    # same index carry only incremental `function.arguments` fragments.
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
                                    "function": {"name": "get_weather", "arguments": ""},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": '{"city":'}}]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": '"NYC"}'}}]
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

    adapter = OpenAIAdapter(api_key="sk-test")
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

    deltas = [e for e in events if isinstance(e, ContentBlockDelta)]
    assert isinstance(deltas[0].delta, InputJsonDelta)
    assert deltas[0].delta.partial_json == '{"city":'
    assert isinstance(deltas[1].delta, InputJsonDelta)
    assert deltas[1].delta.partial_json == '"NYC"}'

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

    adapter = OpenAIAdapter(api_key="sk-test")
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

    adapter = OpenAIAdapter(api_key="sk-test")
    with pytest.raises(RateLimitedError) as exc_info:
        async for _ in adapter.stream(_request()):
            pass

    assert exc_info.value.retry_after_seconds == 30


@respx.mock
async def test_stream_maps_insufficient_quota_to_provider_unavailable() -> None:
    # WHY this exact scenario: confirmed live against the real API during
    # implementation — this is the actual error this key returns.
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

    adapter = OpenAIAdapter(api_key="sk-test")
    with pytest.raises(ProviderUnavailableError):
        async for _ in adapter.stream(_request()):
            pass


@respx.mock
async def test_stream_maps_context_length_exceeded_via_code_field() -> None:
    respx.post(_URL).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "type": "invalid_request_error",
                    "message": "This model's maximum context length is 8192 tokens.",
                    "code": "context_length_exceeded",
                }
            },
        )
    )

    adapter = OpenAIAdapter(api_key="sk-test")
    with pytest.raises(ContextLengthExceededError):
        async for _ in adapter.stream(_request()):
            pass


@respx.mock
async def test_stream_defaults_to_error_stop_reason_when_finish_reason_missing() -> None:
    # WHY this matters: unlike Anthropic's explicit `error` SSE event,
    # OpenAI has no documented mid-stream error frame — a connection that
    # just ends abruptly (no finish_reason ever arrives) is the only signal
    # available, and must not silently look like a normal completion.
    body = _sse(
        [
            {"choices": [{"index": 0, "delta": {"content": "cut off mid"}, "finish_reason": None}]},
        ]
    )
    respx.post(_URL).mock(return_value=httpx.Response(200, content=body))

    adapter = OpenAIAdapter(api_key="sk-test")
    events = [event async for event in adapter.stream(_request())]

    terminal = events[-1]
    assert isinstance(terminal, MessageDelta)
    assert terminal.stop_reason == "error"


@respx.mock
async def test_list_models_translates_flat_list() -> None:
    # WHY a flat list, no pagination: verified live during implementation —
    # unlike Anthropic, GET /v1/models returns everything in one response
    # (125 real entries as of this check, including non-chat models like
    # whisper/tts/embeddings — this adapter doesn't filter those out, since
    # OpenAI's own response carries no field distinguishing them).
    respx.get(_MODELS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "gpt-4o", "object": "model", "owned_by": "openai"},
                    {"id": "gpt-4o-mini", "object": "model", "owned_by": "openai"},
                ],
            },
        )
    )

    adapter = OpenAIAdapter(api_key="sk-test")
    models = await adapter.list_models()

    assert [m.id for m in models] == ["gpt-4o", "gpt-4o-mini"]
    assert models[0].context_window is None


@respx.mock
async def test_list_models_maps_error() -> None:
    respx.get(_MODELS_URL).mock(
        return_value=httpx.Response(
            401,
            json={
                "error": {
                    "type": "authentication_error",
                    "message": "bad key",
                    "code": "invalid_api_key",
                }
            },
        )
    )

    adapter = OpenAIAdapter(api_key="sk-bad")
    with pytest.raises(InternalError) as exc_info:
        await adapter.list_models()
    assert exc_info.value.details["provider"] == "openai"
