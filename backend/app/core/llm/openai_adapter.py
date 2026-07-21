"""OpenAI provider adapter (API_CONTRACT.md §5.5, ADR-0002).

Role: translates LLMRequest into an OpenAI Chat Completions API call and its
SSE stream into normalized LLMEvents.
Called by: app/services/chat.py via app.core.llm.adapter's ProviderAdapter
interface.
Calls: httpx (ADR-0002 decision 1 — not the `openai` SDK), app.core.errors,
app.core.llm.types.
Gotcha: unlike Anthropic, OpenAI's stream never explicitly marks a content
block "done" — there is no content_block_stop equivalent in its wire format.
Block boundaries are synthesized here (a new block starts on the first delta
for a new index; ALL open blocks close together only once `finish_reason`
arrives). Anthropic tells you a block ended as it happens; OpenAI only tells
you the whole turn ended.
Gotcha: `stream_options: {"include_usage": true}` is required in the request
or OpenAI's final chunk carries no usage at all — silently omitting it means
every turn against this provider would compute a cost of $0.
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
    ProviderModel,
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

_API_URL = "https://api.openai.com/v1/chat/completions"
_MODELS_URL = "https://api.openai.com/v1/models"
# WHY read=120.0: matches API_CONTRACT §6's "stream idle timeout: 120s" —
# same reasoning as anthropic_adapter.py's identical constant.
_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)
# WHY a separate, short timeout for list_models(): see
# anthropic_adapter.py's identical constant and its WHY comment.
_MODELS_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

_FINISH_REASON_MAP: dict[str, StopReason] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "content_filter",
}

_ERROR_TYPE_MAP: dict[str, type[DomainError]] = {
    "invalid_request_error": InvalidRequestError,
    # WHY InternalError, not a 4xx client-facing type: these mean our own
    # API key or account is misconfigured — not something the caller can fix
    # by changing their request. Same reasoning as anthropic_adapter.py.
    "authentication_error": InternalError,
    "permission_error": InternalError,
    "not_found_error": InvalidRequestError,
    "rate_limit_error": RateLimitedError,
    "api_error": ProviderError,
    # WHY ProviderUnavailableError, not RateLimitedError: this means the
    # account is out of credits, not hitting a transient rate limit — still
    # `retryable: true` since it resolves once the account is topped up, but
    # a different condition than "slow down and retry in N seconds."
    "insufficient_quota": ProviderUnavailableError,
}

# WHY a separate map keyed on OpenAI's structured `code` field, not message
# text: this is a documented, stable field OpenAI actually returns for this
# exact case — not the message-string sniffing anthropic_adapter.py
# deliberately avoids for context-length errors (which Anthropic doesn't
# expose a structured code for).
_CODE_OVERRIDE_MAP: dict[str, type[DomainError]] = {
    "context_length_exceeded": ContextLengthExceededError,
}


class OpenAIAdapter:
    """ProviderAdapter for OpenAI's Chat Completions API. See adapter.py."""

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

            # WHY track state across chunks, unlike Anthropic's adapter:
            # OpenAI never repeats a tool call's `id`/`name` after the first
            # chunk for that index, and never explicitly closes a block — see
            # module docstring.
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
                    openai_index = tool_call["index"]
                    if openai_index not in tool_call_indices:
                        norm_index = next_index
                        next_index += 1
                        tool_call_indices[openai_index] = norm_index
                        opened.append(norm_index)
                        fn = tool_call.get("function", {})
                        yield ContentBlockStart(
                            index=norm_index,
                            block=ToolUseBlockStart(
                                id=tool_call.get("id", ""), name=fn.get("name", "")
                            ),
                        )
                    norm_index = tool_call_indices[openai_index]
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

    async def list_models(self) -> list[ProviderModel]:
        # GOTCHA: verified live during implementation — GET /v1/models
        # returns {"object": "list", "data": [{"id", "object", "owned_by",
        # ...}]}, a flat (non-paginated) list. No context_window, no
        # capabilities, no pricing in the response.
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=_MODELS_TIMEOUT) as client:
            response = await client.get(_MODELS_URL, headers=headers)
        if response.status_code >= 400:
            _raise_for_error_response(response)
        body = response.json()
        return [ProviderModel(id=m["id"]) for m in body.get("data", [])]


def _iter_sse_data(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Yield each SSE frame's `data:` payload, parsed as JSON.

    WHY skip `[DONE]`: OpenAI's stream ends with a literal `data: [DONE]`
    line, not valid JSON — the connection closes right after, so skipping
    it (rather than treating it as a sentinel to act on) is enough; the
    `async for` over lines ends naturally.
    """

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
        messages.extend(_to_openai_messages(message))

    payload: dict[str, Any] = {
        "model": request.model,
        # WHY max_tokens, not max_completion_tokens: correct for gpt-4o and
        # every non-reasoning chat model this adapter currently targets.
        # OpenAI's newer reasoning models (o1/o3/gpt-5 family) require
        # max_completion_tokens instead — not handled here; revisit if the
        # registry ever points this adapter at one of those.
        "max_tokens": request.params.max_tokens,
        "messages": messages,
        "stream": True,
        # WHY required: see module docstring — without this, the final
        # chunk carries no usage at all.
        "stream_options": {"include_usage": True},
    }
    if request.params.temperature is not None:
        payload["temperature"] = request.params.temperature
    if request.params.top_p is not None:
        payload["top_p"] = request.params.top_p
    if request.params.stop_sequences:
        payload["stop"] = request.params.stop_sequences
    # WHY reasoning_effort and response_format dropped: same reasoning as
    # anthropic_adapter.py — no X-Params-Dropped channel exists yet from
    # core/llm/ back to the router (deferred to when chat.py needs it).
    # response_format actually HAS a real OpenAI equivalent
    # (response_format: {"type": "json_schema", ...}) unlike Anthropic, but
    # wiring that through is real scope, not invented speculatively here.
    if request.tools:
        payload["tools"] = [_to_openai_tool(t) for t in request.tools]
        payload["tool_choice"] = _to_openai_tool_choice(request.tool_choice)
    return payload


def _to_openai_messages(message: LLMMessage) -> list[dict[str, Any]]:
    """One LLMMessage can expand into *multiple* OpenAI messages.

    WHY: unlike Anthropic, OpenAI has no inline tool_result content block —
    a tool result is its own message with role="tool". §3.1 requires
    tool_result blocks to appear before any text block in a user message,
    which is exactly the order this emits them in.
    """
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
                content_parts.append(_to_openai_image_part(block))
            else:
                raise InvalidRequestError(
                    f"Unsupported content block type in a user message: {type(block).__name__}.",
                    code="content.unsupported_block",
                )
        if content_parts:
            result.append({"role": "user", "content": content_parts})
        return result

    # assistant
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
            # WHY dropped, not translated: OpenAI's Chat Completions API has
            # no field for echoing reasoning content back on the next turn
            # (its reasoning models handle this internally, server-side, not
            # via anything this adapter's request shape exposes).
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


def _to_openai_image_part(block: ImageBlock) -> dict[str, Any]:
    source = block.source
    if source.kind == "base64":
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{source.media_type};base64,{source.data}"},
        }
    if source.kind == "url":
        return {"type": "image_url", "image_url": {"url": source.url}}
    # WHY raise, not drop: file_id images reference our own Files API
    # (§5.7), which doesn't exist yet — same reasoning as
    # anthropic_adapter.py's identical check.
    raise InvalidRequestError(
        "Image blocks referencing a file_id are not yet supported.",
        code="content.file_id_unsupported",
    )


def _to_openai_tool(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def _to_openai_tool_choice(tool_choice: str | dict[str, str]) -> str | dict[str, Any]:
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
        "code": f"provider.openai.{code or error_type or 'unknown'}",
        "details": {"provider": "openai", "type": error_type, "code": code},
    }
    if retry_after_seconds is not None:
        kwargs["retry_after_seconds"] = retry_after_seconds
    return error_cls(message or f"OpenAI returned an unmapped error: {error_type}.", **kwargs)


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
