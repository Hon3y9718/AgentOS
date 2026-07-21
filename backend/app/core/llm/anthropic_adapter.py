"""Anthropic provider adapter (API_CONTRACT.md §5.5, ADR-0002).

Role: the first real ProviderAdapter implementation. Translates LLMRequest
into an Anthropic Messages API call and Anthropic's SSE stream into
normalized LLMEvents. This is the adapter the whole §5.5 event vocabulary was
modeled on, so most of the translation below is close to a rename, not a
reshape.
Called by: app/services/chat.py (once it exists) via app.core.llm.adapter's
ProviderAdapter interface.
Calls: httpx (ADR-0002 decision 1 — not the `anthropic` SDK), app.core.errors,
app.core.llm.types.
Gotcha: a mid-stream Anthropic `error` event is signaled by *raising*, not by
a normalized error event in the LLMEvent union (there isn't one — see
adapter.py). The caller distinguishes "nothing came through" from "some
content came through, then it broke" by whether any events were yielded
before the exception propagated out of the `async for`.
See: docs/DECISIONS/0002 Provider Abstraction.md
"""

import json
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, NoReturn

import httpx

from app.core.errors import (
    DomainError,
    InternalError,
    InvalidRequestError,
    PayloadTooLargeError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitedError,
)
from app.core.llm.types import (
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    InputJsonDelta,
    LLMEvent,
    LLMMessage,
    LLMRequest,
    LLMUsage,
    MessageDelta,
    ReasoningBlockStart,
    ReasoningDelta,
    TextBlockStart,
    TextDelta,
    ToolDefinition,
    ToolUseBlockStart,
)
from app.schemas.content_block import (
    ContentBlock,
    ImageBlock,
    ReasoningBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from app.schemas.message import StopReason

_API_URL = "https://api.anthropic.com/v1/messages"
# WHY pinned, not "latest": Anthropic's API is versioned by request header,
# not URL — an unpinned version could change response shape under us with no
# code change on our side to notice. Bump deliberately, not by default drift.
_ANTHROPIC_VERSION = "2023-06-01"
# WHY read=120.0: matches API_CONTRACT §6's "stream idle timeout: 120s" — the
# max gap httpx will tolerate between chunks before raising. The contract's
# other limit, "total stream duration: 900s", is NOT enforced here — that
# needs a wall-clock deadline spanning the whole generator, which belongs to
# whichever layer already has to watch every adapter's stream uniformly (the
# future chat service), not duplicated per adapter.
_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)

_STOP_REASON_MAP: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "max_tokens": "max_tokens",
    "tool_use": "tool_use",
    "stop_sequence": "stop_sequence",
    # WHY content_filter: Anthropic's "refusal" stop reason (model declined
    # to continue on safety grounds) is the closest semantic match in our
    # taxonomy (§3.3) even though the wording differs.
    "refusal": "content_filter",
}

_ERROR_TYPE_MAP: dict[str, type[DomainError]] = {
    "invalid_request_error": InvalidRequestError,
    # WHY InternalError, not a 4xx client-facing type: these mean our own API
    # key or account is misconfigured — not something the caller can fix by
    # changing their request.
    "authentication_error": InternalError,
    "permission_error": InternalError,
    "not_found_error": InvalidRequestError,
    "request_too_large": PayloadTooLargeError,
    "rate_limit_error": RateLimitedError,
    "overloaded_error": ProviderUnavailableError,
}


class AnthropicAdapter:
    """ProviderAdapter for Anthropic's Messages API. See adapter.py."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def stream(self, request: LLMRequest) -> AsyncGenerator[LLMEvent, None]:
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        payload = _build_payload(request)

        # WHY a fresh client per call, not a shared instance: this package
        # has no lifecycle owner yet (no lifespan hook closes it) until
        # chat.py wires an adapter in for real. A shared, pooled client is a
        # later optimization once something owns opening/closing it.
        async with (
            httpx.AsyncClient(timeout=_TIMEOUT) as client,
            client.stream("POST", _API_URL, headers=headers, json=payload) as response,
        ):
            if response.status_code >= 400:
                await response.aread()
                _raise_for_error_response(response)

            input_tokens = 0
            cache_read_tokens = 0
            cache_write_tokens = 0
            output_tokens = 0
            stop_reason: str | None = None

            async for event in _iter_sse_data(response):
                event_type = event.get("type")

                if event_type == "message_start":
                    usage = event["message"]["usage"]
                    input_tokens = usage.get("input_tokens", 0)
                    cache_read_tokens = usage.get("cache_read_input_tokens", 0) or 0
                    cache_write_tokens = usage.get("cache_creation_input_tokens", 0) or 0

                elif event_type == "content_block_start":
                    yield _to_content_block_start(event)

                elif event_type == "content_block_delta":
                    delta_event = _to_content_block_delta(event)
                    if delta_event is not None:
                        yield delta_event

                elif event_type == "content_block_stop":
                    yield ContentBlockStop(index=event["index"])

                elif event_type == "message_delta":
                    stop_reason = event["delta"].get("stop_reason")
                    output_tokens = event["usage"].get("output_tokens", 0)

                elif event_type == "message_stop":
                    yield MessageDelta(
                        stop_reason=_STOP_REASON_MAP.get(stop_reason or "", "error"),
                        usage=LLMUsage(
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            cache_read_tokens=cache_read_tokens,
                            cache_write_tokens=cache_write_tokens,
                        ),
                    )

                elif event_type == "error":
                    error = event["error"]
                    raise _map_error(error.get("type", ""), error.get("message", ""))

                # WHY no `else: raise`: unrecognized event types (a future
                # Anthropic API version adding one) are ignored, not fatal —
                # §7's client-obligation "ignore unknown event names" applies
                # just as much to this adapter reading Anthropic's stream as
                # it does to a client reading ours.


def _iter_sse_data(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Yield each SSE frame's `data:` payload, parsed as JSON.

    WHY ignore `event:` lines entirely: Anthropic's data payload always
    carries its own `"type"` field matching the event name, so dispatching
    off the parsed JSON is equivalent and simpler than tracking SSE event
    names as separate state.
    """

    async def _gen() -> AsyncIterator[dict[str, Any]]:
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            raw = line.removeprefix("data:").strip()
            if raw:
                yield json.loads(raw)

    return _gen()


def _build_payload(request: LLMRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": request.model,
        "max_tokens": request.params.max_tokens,
        "messages": [_to_anthropic_message(m) for m in request.messages],
        "stream": True,
    }
    if request.system_prompt is not None:
        payload["system"] = request.system_prompt
    if request.params.temperature is not None:
        payload["temperature"] = request.params.temperature
    if request.params.top_p is not None:
        payload["top_p"] = request.params.top_p
    if request.params.stop_sequences:
        payload["stop_sequences"] = request.params.stop_sequences
    # WHY reasoning_effort and response_format are silently dropped: neither
    # maps onto a Messages API parameter directly (reasoning_effort would
    # need a token-budget heuristic for Anthropic's separate "extended
    # thinking" feature; response_format has no Anthropic equivalent at all).
    # §5.4 expects a dropped param to surface as an `X-Params-Dropped`
    # response header, but core/llm/ has no channel back to the router for
    # that yet (stream() only yields LLMEvents) — deferred to when chat.py
    # actually needs to report this, not invented speculatively here.
    if request.tools:
        payload["tools"] = [_to_anthropic_tool(t) for t in request.tools]
        payload["tool_choice"] = _to_anthropic_tool_choice(request.tool_choice)
    return payload


def _to_anthropic_message(message: LLMMessage) -> dict[str, Any]:
    return {
        "role": message.role,
        "content": [_to_anthropic_block(block) for block in message.content],
    }


def _to_anthropic_block(block: ContentBlock) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}

    if isinstance(block, ImageBlock):
        source = block.source
        if source.kind == "base64":
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": source.media_type,
                    "data": source.data,
                },
            }
        if source.kind == "url":
            return {"type": "image", "source": {"type": "url", "url": source.url}}
        # WHY raise, not drop: file_id images reference our own Files API
        # (§5.7), which doesn't exist yet (roadmap: files.py unbuilt) — there
        # is no way to resolve a file_id into bytes Anthropic can accept.
        raise InvalidRequestError(
            "Image blocks referencing a file_id are not yet supported.",
            code="content.file_id_unsupported",
        )

    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}

    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "is_error": block.is_error,
            "content": [{"type": "text", "text": part.text} for part in block.content],
        }

    if isinstance(block, ReasoningBlock):
        if block.redacted:
            return {"type": "redacted_thinking", "data": block.text}
        return {"type": "thinking", "thinking": block.text, "signature": block.signature or ""}

    raise InvalidRequestError(
        f"Unsupported content block type: {type(block).__name__}.",
        code="content.unsupported_block",
    )


def _to_anthropic_tool(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
    }


def _to_anthropic_tool_choice(tool_choice: str | dict[str, str]) -> dict[str, Any]:
    if tool_choice == "none":
        return {"type": "none"}
    if tool_choice == "required":
        return {"type": "any"}
    if isinstance(tool_choice, dict):
        return {"type": "tool", "name": tool_choice["name"]}
    return {"type": "auto"}


def _to_content_block_start(event: dict[str, Any]) -> ContentBlockStart:
    block = event["content_block"]
    index = event["index"]
    block_type = block["type"]

    if block_type == "text":
        return ContentBlockStart(index=index, block=TextBlockStart())
    if block_type == "tool_use":
        return ContentBlockStart(
            index=index, block=ToolUseBlockStart(id=block["id"], name=block["name"])
        )
    if block_type in ("thinking", "redacted_thinking"):
        return ContentBlockStart(index=index, block=ReasoningBlockStart())

    # WHY ProviderError, not InvalidRequestError: an unrecognized block type
    # here means Anthropic's API returned something this adapter doesn't
    # know how to translate — that's an upstream/adapter mismatch to triage
    # (§2's rule for unmapped provider behavior), not a bad client request.
    raise ProviderError(
        f"Unrecognized Anthropic content block type: {block_type!r}.",
        code="provider.unrecognized_block_type",
        details={"provider": "anthropic", "block_type": block_type},
    )


def _to_content_block_delta(event: dict[str, Any]) -> ContentBlockDelta | None:
    delta = event["delta"]
    index = event["index"]
    delta_type = delta["type"]

    if delta_type == "text_delta":
        return ContentBlockDelta(index=index, delta=TextDelta(text=delta["text"]))
    if delta_type == "input_json_delta":
        return ContentBlockDelta(
            index=index, delta=InputJsonDelta(partial_json=delta["partial_json"])
        )
    if delta_type == "thinking_delta":
        return ContentBlockDelta(index=index, delta=ReasoningDelta(text=delta["thinking"]))
    if delta_type == "signature_delta":
        # WHY None, not a ReasoningDelta: a signature fragment carries no
        # displayable text (§3.1's ReasoningBlock.signature is opaque
        # provider state, not content) — nothing in our normalized vocabulary
        # represents it mid-stream. Anthropic's own API accumulates it into
        # the final message, which we're not required to re-emit as a delta.
        return None

    raise ProviderError(
        f"Unrecognized Anthropic delta type: {delta_type!r}.",
        code="provider.unrecognized_delta_type",
        details={"provider": "anthropic", "delta_type": delta_type},
    )


def _map_error(
    error_type: str, message: str, *, retry_after_seconds: int | None = None
) -> DomainError:
    error_cls = _ERROR_TYPE_MAP.get(error_type, ProviderError)
    kwargs: dict[str, Any] = {
        "code": f"provider.anthropic.{error_type or 'unknown'}",
        "details": {"provider": "anthropic", "code": error_type},
    }
    if retry_after_seconds is not None:
        kwargs["retry_after_seconds"] = retry_after_seconds
    return error_cls(message or f"Anthropic returned an unmapped error: {error_type}.", **kwargs)


def _raise_for_error_response(response: httpx.Response) -> NoReturn:
    try:
        body = response.json()
        error = body.get("error", {})
        error_type = error.get("type", "")
        message = error.get("message", "")
    except (json.JSONDecodeError, ValueError):
        error_type = ""
        message = response.text

    # WHY only parsed here, not in the mid-stream error path: a mid-stream
    # `error` SSE event carries no HTTP headers (the response headers were
    # already sent with the initial 200) — Retry-After only exists to parse
    # on the pre-stream failure path.
    retry_after_seconds = None
    retry_after_header = response.headers.get("retry-after")
    if retry_after_header is not None and retry_after_header.isdigit():
        retry_after_seconds = int(retry_after_header)

    raise _map_error(error_type, message, retry_after_seconds=retry_after_seconds)
