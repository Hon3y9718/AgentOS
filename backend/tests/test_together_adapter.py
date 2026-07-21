"""app/core/llm/together_adapter.py (API_CONTRACT.md §5.5, ADR-0002).

Contract tier (ARCHITECTURE.md): fixture-driven, mocked transport, no real
network. Unlike test_groq_adapter.py/test_openai_adapter.py, this file was
NOT live-verified against the real Together API — it was written after the
fact, from the adapter module's own docstring/comments (which do claim live
verification of the usage-field location and error codes; see BUILD_LOG for
this session). Fixtures here encode what those comments assert, not a fresh
live check.
"""

import json

import httpx
import pytest
import respx

from app.core.errors import InvalidRequestError, ProviderUnavailableError, RateLimitedError
from app.core.llm.together_adapter import TogetherAdapter
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

_URL = "https://api.together.xyz/v1/chat/completions"


def _sse(chunks: list[dict[str, object]]) -> bytes:
    frames = [f"data: {json.dumps(c)}\n\n" for c in chunks]
    frames.append("data: [DONE]\n\n")
    return "".join(frames).encode()


def _request(**overrides: object) -> LLMRequest:
    defaults: dict[str, object] = {
        "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "system_prompt": None,
        "messages": [LLMMessage(role="user", content=[TextBlock(text="hi")])],
        "params": LLMParams(max_tokens=1024),
    }
    defaults.update(overrides)
    return LLMRequest(**defaults)  # type: ignore[arg-type]


@respx.mock
async def test_stream_translates_text_response() -> None:
    # WHY usage lands on the finish_reason chunk, not a trailing
    # empty-choices chunk: this is Together's actual wire shape (see the
    # adapter's own module docstring) — different from OpenAI/Groq, and
    # this fixture exercises that difference rather than assuming it away.
    body = _sse(
        [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"content": "Hel"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": None}]},
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        ]
    )
    respx.post(_URL).mock(return_value=httpx.Response(200, content=body))

    adapter = TogetherAdapter(api_key="together-test")
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
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 12},
            },
        ]
    )
    respx.post(_URL).mock(return_value=httpx.Response(200, content=body))

    adapter = TogetherAdapter(api_key="together-test")
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
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1024},
            },
        ]
    )
    respx.post(_URL).mock(return_value=httpx.Response(200, content=body))

    adapter = TogetherAdapter(api_key="together-test")
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

    adapter = TogetherAdapter(api_key="together-test")
    with pytest.raises(RateLimitedError) as exc_info:
        async for _ in adapter.stream(_request()):
            pass

    assert exc_info.value.retry_after_seconds == 30


@respx.mock
async def test_stream_maps_model_not_available_to_invalid_request() -> None:
    # WHY this exact code, unlike Groq's "model_not_found": confirmed live
    # against the real API during implementation — Together's actual code
    # for an unknown/inaccessible model (see the adapter's own
    # _CODE_OVERRIDE_MAP comment).
    respx.post(_URL).mock(
        return_value=httpx.Response(
            404,
            json={
                "error": {
                    "message": "Unable to access model not-a-real-model",
                    "type": "invalid_request_error",
                    "code": "model_not_available",
                }
            },
        )
    )

    adapter = TogetherAdapter(api_key="together-test")
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

    adapter = TogetherAdapter(api_key="together-test")
    with pytest.raises(ProviderUnavailableError):
        async for _ in adapter.stream(_request()):
            pass
