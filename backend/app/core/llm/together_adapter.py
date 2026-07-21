"""Together AI provider adapter (API_CONTRACT.md §5.5, ADR-0002).

Role: translates LLMRequest into a Together AI Chat Completions API call and
its SSE stream into normalized LLMEvents. Together's API is documented as
OpenAI-compatible, and this file is a deliberate near-duplicate of
openai_adapter.py/groq_adapter.py rather than a shared base class (asked and
confirmed — see groq_adapter.py's module docstring for the same reasoning).
Called by: app/services/chat.py via app.core.llm.adapter's ProviderAdapter
interface.
Calls: httpx (ADR-0002 decision 1 — no provider SDK), app.core.errors,
app.core.llm.types.
Gotcha: same content-block-boundary synthesis as openai_adapter.py — no
explicit block-close signal in the wire format.
Gotcha: verified live (not assumed) during implementation that Together
puts `"usage"` directly on the *final content* chunk (the one carrying
`finish_reason`), not on a separate `{"choices": [], "usage": {...}}` chunk
the way OpenAI/Groq do. This adapter's `if chunk.get("usage")` check on
every chunk handles both shapes identically without needing a special case
— worth knowing if a future refactor is tempted to assume usage always
arrives on its own chunk.
See: docs/DECISIONS/0002 Provider Abstraction.md
"""

import json
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, NoReturn

import httpx

from app.core.errors import (
    ContextLengthExceededError,
    DomainError,
    InternalError,
    InvalidRequestError,
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
    TextBlockStart,
    TextDelta,
    ToolDefinition,
    ToolUseBlockStart,
)
from app.schemas.content_block import (
    ImageBlock,
    ReasoningBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from app.schemas.message import StopReason

_API_URL = "https://api.together.xyz/v1/chat/completions"
# WHY read=120.0: matches API_CONTRACT §6's "stream idle timeout: 120s" —
# same reasoning as anthropic_adapter.py's identical constant.
_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)

_FINISH_REASON_MAP: dict[str, StopReason] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "content_filter",
    # WHY "eos" too: Together's own docs list this as an alternative to
    # "stop" for some models' native end-of-sequence token — same semantic
    # meaning (natural completion), not encountered live but documented.
    "eos": "end_turn",
}

_ERROR_TYPE_MAP: dict[str, type[DomainError]] = {
    "invalid_request_error": InvalidRequestError,
    "authentication_error": InternalError,
    "permission_error": InternalError,
    "not_found_error": InvalidRequestError,
    "rate_limit_error": RateLimitedError,
    "api_error": ProviderError,
    "insufficient_quota": ProviderUnavailableError,
}

_CODE_OVERRIDE_MAP: dict[str, type[DomainError]] = {
    "context_length_exceeded": ContextLengthExceededError,
    # WHY explicit, even though "invalid_request_error" already lands here
    # via the type map: verified live during implementation — this is
    # Together's actual code for an unknown/inaccessible model (HTTP 404).
    "model_not_available": InvalidRequestError,
}


class TogetherAdapter:
    """ProviderAdapter for Together AI's Chat Completions API. See adapter.py."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def stream(self, request: LLMRequest) -> AsyncGenerator[LLMEvent, None]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = _build_payload(request)

        async with (
            httpx.AsyncClient(timeout=_TIMEOUT) as client,
            client.stream("POST", _API_URL, headers=headers, json=payload) as response,
        ):
            if response.status_code >= 400:
                await response.aread()
                _raise_for_error_response(response)

            text_index: int | None = None
            tool_call_indices: dict[int, int] = {}
            next_index = 0
            opened: list[int] = []
            finish_reason: str | None = None
            usage: dict[str, Any] = {}

            async for chunk in _iter_sse_data(response):
                if chunk.get("usage"):
                    usage = chunk["usage"]

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta", {})

                content = delta.get("content")
                if content:
                    if text_index is None:
                        text_index = next_index
                        next_index += 1
                        opened.append(text_index)
                        yield ContentBlockStart(index=text_index, block=TextBlockStart())
                    yield ContentBlockDelta(index=text_index, delta=TextDelta(text=content))

                for tool_call in delta.get("tool_calls") or []:
                    together_index = tool_call["index"]
                    if together_index not in tool_call_indices:
                        norm_index = next_index
                        next_index += 1
                        tool_call_indices[together_index] = norm_index
                        opened.append(norm_index)
                        fn = tool_call.get("function", {})
                        yield ContentBlockStart(
                            index=norm_index,
                            block=ToolUseBlockStart(
                                id=tool_call.get("id", ""), name=fn.get("name", "")
                            ),
                        )
                    norm_index = tool_call_indices[together_index]
                    fragment = tool_call.get("function", {}).get("arguments")
                    if fragment:
                        yield ContentBlockDelta(
                            index=norm_index, delta=InputJsonDelta(partial_json=fragment)
                        )

                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]

            for index in opened:
                yield ContentBlockStop(index=index)

            yield MessageDelta(
                stop_reason=_FINISH_REASON_MAP.get(finish_reason or "", "error"),
                usage=LLMUsage(
                    input_tokens=usage.get("prompt_tokens", 0),
                    output_tokens=usage.get("completion_tokens", 0),
                ),
            )


def _iter_sse_data(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    async def _gen() -> AsyncIterator[dict[str, Any]]:
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            raw = line.removeprefix("data:").strip()
            if not raw or raw == "[DONE]":
                continue
            yield json.loads(raw)

    return _gen()


def _build_payload(request: LLMRequest) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    if request.system_prompt is not None:
        messages.append({"role": "system", "content": request.system_prompt})
    for message in request.messages:
        messages.extend(_to_together_messages(message))

    payload: dict[str, Any] = {
        "model": request.model,
        "max_tokens": request.params.max_tokens,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if request.params.temperature is not None:
        payload["temperature"] = request.params.temperature
    if request.params.top_p is not None:
        payload["top_p"] = request.params.top_p
    if request.params.stop_sequences:
        payload["stop"] = request.params.stop_sequences
    if request.tools:
        payload["tools"] = [_to_together_tool(t) for t in request.tools]
        payload["tool_choice"] = _to_together_tool_choice(request.tool_choice)
    return payload


def _to_together_messages(message: LLMMessage) -> list[dict[str, Any]]:
    """One LLMMessage can expand into *multiple* Together messages — same
    tool_result-becomes-its-own-message shape as OpenAI/Groq."""
    if message.role == "user":
        result: list[dict[str, Any]] = []
        content_parts: list[dict[str, Any]] = []
        for block in message.content:
            if isinstance(block, ToolResultBlock):
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.tool_use_id,
                        "content": "\n".join(part.text for part in block.content),
                    }
                )
            elif isinstance(block, TextBlock):
                content_parts.append({"type": "text", "text": block.text})
            elif isinstance(block, ImageBlock):
                content_parts.append(_to_together_image_part(block))
            else:
                raise InvalidRequestError(
                    f"Unsupported content block type in a user message: {type(block).__name__}.",
                    code="content.unsupported_block",
                )
        if content_parts:
            result.append({"role": "user", "content": content_parts})
        return result

    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            text_parts.append(block.text)
        elif isinstance(block, ToolUseBlock):
            tool_calls.append(
                {
                    "id": block.id,
                    "type": "function",
                    "function": {"name": block.name, "arguments": json.dumps(block.input)},
                }
            )
        elif isinstance(block, ReasoningBlock):
            continue
        else:
            raise InvalidRequestError(
                f"Unsupported content block type in an assistant message: {type(block).__name__}.",
                code="content.unsupported_block",
            )
    assistant_message: dict[str, Any] = {
        "role": "assistant",
        "content": "\n".join(text_parts) or None,
    }
    if tool_calls:
        assistant_message["tool_calls"] = tool_calls
    return [assistant_message]


def _to_together_image_part(block: ImageBlock) -> dict[str, Any]:
    source = block.source
    if source.kind == "base64":
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{source.media_type};base64,{source.data}"},
        }
    if source.kind == "url":
        return {"type": "image_url", "image_url": {"url": source.url}}
    raise InvalidRequestError(
        "Image blocks referencing a file_id are not yet supported.",
        code="content.file_id_unsupported",
    )


def _to_together_tool(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def _to_together_tool_choice(tool_choice: str | dict[str, str]) -> str | dict[str, Any]:
    if tool_choice in ("none", "required"):
        return tool_choice
    if isinstance(tool_choice, dict):
        return {"type": "function", "function": {"name": tool_choice["name"]}}
    return "auto"


def _map_error(
    error_type: str, code: str, message: str, *, retry_after_seconds: int | None = None
) -> DomainError:
    error_cls = _CODE_OVERRIDE_MAP.get(code) or _ERROR_TYPE_MAP.get(error_type, ProviderError)
    kwargs: dict[str, Any] = {
        "code": f"provider.together.{code or error_type or 'unknown'}",
        "details": {"provider": "together", "type": error_type, "code": code},
    }
    if retry_after_seconds is not None:
        kwargs["retry_after_seconds"] = retry_after_seconds
    return error_cls(message or f"Together AI returned an unmapped error: {error_type}.", **kwargs)


def _raise_for_error_response(response: httpx.Response) -> NoReturn:
    try:
        body = response.json()
        error = body.get("error", {})
        error_type = error.get("type", "")
        code = error.get("code") or ""
        message = error.get("message", "")
    except (json.JSONDecodeError, ValueError):
        error_type = ""
        code = ""
        message = response.text

    retry_after_seconds = None
    retry_after_header = response.headers.get("retry-after")
    if retry_after_header is not None and retry_after_header.isdigit():
        retry_after_seconds = int(retry_after_header)

    raise _map_error(error_type, code, message, retry_after_seconds=retry_after_seconds)
