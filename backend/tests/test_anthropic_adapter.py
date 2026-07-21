"""app/core/llm/anthropic_adapter.py (API_CONTRACT.md §5.5, ADR-0002).

Contract tier (ARCHITECTURE.md): fixture-driven, mocked transport, no real
network — a live smoke test against the real Anthropic API is roadmap item 8,
run manually, excluded from CI.
"""

import json

import httpx
import pytest
import respx

from app.core.errors import ProviderUnavailableError, RateLimitedError
from app.core.llm.anthropic_adapter import AnthropicAdapter
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

_URL = "https://api.anthropic.com/v1/messages"


def _sse(events: list[dict[str, object]]) -> bytes:
    frames = [f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events]
    return "".join(frames).encode()


def _request(**overrides: object) -> LLMRequest:
    defaults: dict[str, object] = {
        "model": "claude-sonnet-4-5",
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
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 10}},
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hel"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "lo"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 5},
            },
            {"type": "message_stop"},
        ]
    )
    respx.post(_URL).mock(return_value=httpx.Response(200, content=body))

    adapter = AnthropicAdapter(api_key="sk-test")
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
            {"type": "message_start", "message": {"usage": {"input_tokens": 20}}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "get_weather"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"city":'},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '"NYC"}'},
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
    respx.post(_URL).mock(return_value=httpx.Response(200, content=body))

    adapter = AnthropicAdapter(api_key="sk-test")
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
    assert start.block.id == "toolu_1"
    assert start.block.name == "get_weather"

    deltas = [e for e in events if isinstance(e, ContentBlockDelta)]
    assert isinstance(deltas[0].delta, InputJsonDelta)
    assert deltas[0].delta.partial_json == '{"city":'

    terminal = events[-1]
    assert isinstance(terminal, MessageDelta)
    assert terminal.stop_reason == "tool_use"


@respx.mock
async def test_stream_maps_max_tokens_stop_reason() -> None:
    body = _sse(
        [
            {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "cut off"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "max_tokens"},
                "usage": {"output_tokens": 1024},
            },
            {"type": "message_stop"},
        ]
    )
    respx.post(_URL).mock(return_value=httpx.Response(200, content=body))

    adapter = AnthropicAdapter(api_key="sk-test")
    events = [event async for event in adapter.stream(_request())]

    terminal = events[-1]
    assert isinstance(terminal, MessageDelta)
    assert terminal.stop_reason == "max_tokens"


@respx.mock
async def test_stream_maps_pre_stream_rate_limit_error() -> None:
    respx.post(_URL).mock(
        return_value=httpx.Response(
            429,
            json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
            headers={"retry-after": "30"},
        )
    )

    adapter = AnthropicAdapter(api_key="sk-test")
    with pytest.raises(RateLimitedError) as exc_info:
        async for _ in adapter.stream(_request()):
            pass

    assert exc_info.value.retry_after_seconds == 30
    assert exc_info.value.details == {"provider": "anthropic", "code": "rate_limit_error"}


@respx.mock
async def test_stream_raises_on_mid_stream_error_event_after_partial_content() -> None:
    body = _sse(
        [
            {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "partial"},
            },
            {
                "type": "error",
                "error": {"type": "overloaded_error", "message": "Overloaded"},
            },
        ]
    )
    respx.post(_URL).mock(return_value=httpx.Response(200, content=body))

    adapter = AnthropicAdapter(api_key="sk-test")
    received: list[object] = []
    with pytest.raises(ProviderUnavailableError):
        async for event in adapter.stream(_request()):
            received.append(event)

    # WHY this assertion matters: it's how a caller distinguishes "nothing
    # came through" from "some content came through, then it broke" (see
    # anthropic_adapter.py's module docstring gotcha).
    assert len(received) == 2
