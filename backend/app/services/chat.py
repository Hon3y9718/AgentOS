"""Chat — the core turn (API_CONTRACT.md §5.4, §5.5).

Role: where conversations, messages, and core/llm meet (ARCHITECTURE.md).
Persists the user message, resolves model/params, loads history, calls the
resolved provider adapter, accumulates its normalized event stream, computes
cost, and persists the assistant message — for both the non-streaming
(`Accept: application/json`, `create_chat_message`) and streaming
(`Accept: text/event-stream`, `prepare_stream` + `emit_stream`) response
shapes. No `fastapi` import (ARCHITECTURE.md's layering rule) — `emit_stream`
takes a plain `is_disconnected` callable, not a `Request` object, for exactly
that reason.
Called by: app/api/v1/chat.py. Calls app.services.conversations,
app.services.messages, app.services.idempotency, app.services.users,
app.core.llm.*, app.models.message, app.models.conversation, app.core.errors.
Gotcha: `prepare_stream()` (validation, idempotency claim, persistence) and
`emit_stream()` (frame emission) are deliberately two separate awaitables,
not one generator. §5.5: "errors that occur before the first byte use the
normal error envelope with a real HTTP status" — only an eagerly-`await`ed
call, run *before* `StreamingResponse` is constructed, can still produce a
clean pre-stream HTTP error. A `DomainError` raised from inside an async
generator after the router has already returned a `StreamingResponse` can't
retroactively change a status code that may already be on the wire.
See: docs/DECISIONS/0002 Provider Abstraction.md
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, NamedTuple

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors import DomainError, InvalidRequestError, ProviderUnavailableError
from app.core.ids import new_id
from app.core.llm.adapter import ADAPTER_CLASSES, ProviderAdapter
from app.core.llm.pricing import compute_cost_usd
from app.core.llm.registry import ModelEntry, registry
from app.core.llm.types import (
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    InputJsonDelta,
    LLMEvent,
    LLMMessage,
    LLMParams,
    LLMRequest,
    LLMUsage,
    MessageDelta,
    ReasoningBlockStart,
    ReasoningDelta,
    TextBlockStart,
    TextDelta,
    ToolUseBlockStart,
)
from app.models.conversation import Conversation as ConversationModel
from app.models.message import Message as MessageModel
from app.schemas.chat import ChatParams, ChatRequest, ChatResponse
from app.schemas.content_block import ContentBlock, ReasoningBlock, TextBlock, ToolUseBlock
from app.schemas.conversation import Conversation
from app.services import conversations as conversations_service
from app.services import idempotency
from app.services import users as users_service
from app.services.messages import row_to_schema

# WHY 4096: API_CONTRACT §4's own worked example uses this as max_tokens —
# the closest thing to a stated convention when neither the request nor the
# conversation's default_params specifies one.
_DEFAULT_MAX_TOKENS = 4096

# WHY a module-level constant, not a literal inline: tests override this
# (monkeypatch) to avoid a real 15-second wait when exercising the ping path.
PING_INTERVAL_SECONDS = 15.0

_content_block_list_adapter = TypeAdapter(list[ContentBlock])


async def create_chat_message(
    db: AsyncSession,
    user_id: str,
    conversation_id: str,
    idempotency_key: str,
    data: ChatRequest,
) -> ChatResponse:
    """Run one chat turn, non-streaming (§5.4's `Accept: application/json`).

    Raises:
        app.core.errors.DomainError: a subclass matching §2 — NotFoundError
            (bad conversation), InvalidRequestError (bad/missing model,
            unsupported `tools`), ProviderUnavailableError (provider not
            configured, or raised by the adapter itself), ConflictError
            (Idempotency-Key reuse — see app.services.idempotency), or
            whatever the adapter raises for an upstream failure.
    """
    conversation, entry, params = await _validate_and_resolve(db, user_id, conversation_id, data)

    request_hash = idempotency.hash_request_body(data.model_dump(mode="json"))
    existing = await idempotency.check_or_claim(
        db,
        user_id=user_id,
        conversation_id=conversation_id,
        key=idempotency_key,
        request_hash=request_hash,
    )
    if existing is not None:
        assert existing.response_body is not None, "a 'complete' idempotency row always has one"
        return ChatResponse.model_validate(existing.response_body)

    llm_request, user_row, assistant_row = await _persist_turn_start(
        db, conversation, entry, params, data
    )

    try:
        result = await _run_turn(db, user_id, entry, llm_request, user_row, assistant_row)
    except DomainError:
        await idempotency.abandon(db, key=idempotency_key)
        raise
    else:
        await idempotency.complete(
            db,
            key=idempotency_key,
            response_status=201,
            response_body=result.model_dump(mode="json"),
        )
        return result


class _ReplayPlan(NamedTuple):
    """`prepare_stream()`'s result when this is a valid idempotency replay."""

    response: ChatResponse


class _FreshPlan(NamedTuple):
    """`prepare_stream()`'s result when this is a genuinely new turn."""

    idempotency_key: str
    # WHY carried here, not re-derived in emit_stream: emit_stream() only
    # receives `plan`, not the router's original user_id — this is how it
    # learns which user's tokens_used to increment on the success path,
    # without widening its own parameter list.
    user_id: str
    entry: ModelEntry
    llm_request: LLMRequest
    user_row: MessageModel
    assistant_row: MessageModel


StreamPlan = _ReplayPlan | _FreshPlan


async def prepare_stream(
    db: AsyncSession,
    user_id: str,
    conversation_id: str,
    idempotency_key: str,
    data: ChatRequest,
) -> StreamPlan:
    """Everything for §5.5 that must happen *before* the SSE response starts.

    Callers must `await` this directly (not inside a generator) so a
    `DomainError` here still produces a normal pre-stream HTTP error — see
    module docstring. Raises the same errors as `create_chat_message`.
    """
    conversation, entry, params = await _validate_and_resolve(db, user_id, conversation_id, data)

    request_hash = idempotency.hash_request_body(data.model_dump(mode="json"))
    existing = await idempotency.check_or_claim(
        db,
        user_id=user_id,
        conversation_id=conversation_id,
        key=idempotency_key,
        request_hash=request_hash,
    )
    if existing is not None:
        assert existing.response_body is not None, "a 'complete' idempotency row always has one"
        return _ReplayPlan(response=ChatResponse.model_validate(existing.response_body))

    llm_request, user_row, assistant_row = await _persist_turn_start(
        db, conversation, entry, params, data
    )
    return _FreshPlan(
        idempotency_key=idempotency_key,
        user_id=user_id,
        entry=entry,
        llm_request=llm_request,
        user_row=user_row,
        assistant_row=assistant_row,
    )


async def emit_stream(
    db: AsyncSession,
    plan: StreamPlan,
    *,
    request_id: str | None,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[str]:
    """Yield raw SSE frame strings (§5.5). Only ever called after
    `prepare_stream()` has already succeeded — everything here happens
    after headers are sent, so a `DomainError` is framed as an `error`
    event, never raised (see module docstring).

    Gotcha: a replay does NOT re-emit the original stream's exact chunking
    — it reconstructs a single-shot SSE sequence from the same
    final-response data `create_chat_message` stores for the non-streaming
    path. §7's client obligations (buffer deltas until content_block_stop,
    ignore unknown events) mean a client can't tell the difference, and
    reusing that storage shape outright avoids a second, larger idempotency
    format just for streams.
    """
    if isinstance(plan, _ReplayPlan):
        for replay_frame in _replay_frames(plan.response):
            yield replay_frame
        return

    run_id = new_id("run")
    yield _frame(
        "message_start",
        {
            "user_message_id": plan.user_row.id,
            "assistant_message_id": plan.assistant_row.id,
            "model": plan.entry.id,
            "run_id": run_id,
        },
    )

    accumulator = _ContentAccumulator()
    agen = _get_adapter(plan.entry).stream(plan.llm_request)
    terminal: MessageDelta | None = None
    disconnected = False
    # WHY asyncio.wait(), not wait_for(), around agen.__anext__(): verified
    # empirically before writing this — wait_for() *cancels* the wrapped
    # coroutine on timeout, and cancelling an async generator's in-flight
    # __anext__() permanently exhausts it (the next call raises
    # StopAsyncIteration immediately, looking like a clean end-of-stream
    # instead of "the provider was just slow"). asyncio.wait() leaves a
    # timed-out task alive and still running in the background; re-waiting
    # on the *same* task across repeated ping timeouts lets it eventually
    # complete with the real event once the provider actually sends one.
    pending_task: asyncio.Task[LLMEvent] | None = None

    try:
        while True:
            if await is_disconnected():
                disconnected = True
                if pending_task is not None:
                    pending_task.cancel()
                    with contextlib.suppress(BaseException):
                        await pending_task
                await agen.aclose()
                break

            if pending_task is None:
                pending_task = asyncio.ensure_future(agen.__anext__())

            done, _pending = await asyncio.wait({pending_task}, timeout=PING_INTERVAL_SECONDS)
            if not done:
                yield _frame("ping", {})
                continue

            pending_task = None
            try:
                event = done.pop().result()
            except StopAsyncIteration:
                break

            frame, flush = _handle_event(accumulator, event)
            if frame is not None:
                yield frame
            if flush:
                plan.assistant_row.content = accumulator.finalize()
                await db.commit()
            if isinstance(event, MessageDelta):
                terminal = event
    except DomainError as exc:
        plan.assistant_row.content = accumulator.finalize()
        plan.assistant_row.status = "failed"
        plan.assistant_row.completed_at = datetime.now(UTC).replace(tzinfo=None)
        await db.commit()
        await idempotency.abandon(db, key=plan.idempotency_key)
        yield _frame("error", {"error": exc.to_envelope(request_id)})
        yield _frame("message_stop", {"status": "failed"})
        return

    if disconnected:
        # WHY nothing more is yielded: the client is gone — there is no
        # connection left to send message_delta/message_stop over.
        plan.assistant_row.content = accumulator.finalize()
        plan.assistant_row.status = "incomplete"
        plan.assistant_row.stop_reason = "cancelled"
        plan.assistant_row.completed_at = datetime.now(UTC).replace(tzinfo=None)
        await db.commit()
        await idempotency.abandon(db, key=plan.idempotency_key)
        return

    assert terminal is not None, "adapter.stream() ended without a terminal MessageDelta"
    plan.assistant_row.content = accumulator.finalize()
    plan.assistant_row.status = "complete"
    plan.assistant_row.stop_reason = terminal.stop_reason
    plan.assistant_row.usage = _usage_dict(terminal.usage, plan.entry)
    plan.assistant_row.completed_at = datetime.now(UTC).replace(tzinfo=None)
    await users_service.increment_tokens_used(
        db, plan.user_id, terminal.usage.input_tokens + terminal.usage.output_tokens
    )
    await db.commit()

    result = ChatResponse(
        user_message=row_to_schema(plan.user_row, include_reasoning=True),
        assistant_message=row_to_schema(plan.assistant_row, include_reasoning=True),
    )
    await idempotency.complete(
        db,
        key=plan.idempotency_key,
        response_status=200,
        response_body=result.model_dump(mode="json"),
    )
    yield _frame("message_stop", {"status": "complete"})


def _frame(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _handle_event(accumulator: "_ContentAccumulator", event: LLMEvent) -> tuple[str | None, bool]:
    """Apply one adapter event to the accumulator; return the SSE frame to
    emit (if any) and whether this event is a natural DB-flush checkpoint."""
    if isinstance(event, ContentBlockStart):
        accumulator.start(event)
        return _frame(
            "content_block_start",
            {"index": event.index, "block": event.block.model_dump(mode="json")},
        ), False
    if isinstance(event, ContentBlockDelta):
        accumulator.delta(event)
        return _frame(
            "content_block_delta",
            {"index": event.index, "delta": event.delta.model_dump(mode="json")},
        ), False
    if isinstance(event, ContentBlockStop):
        accumulator.stop(event)
        # WHY flush here: a block boundary is a natural checkpoint — matches
        # ARCHITECTURE.md's "accumulate + periodically flush... not per
        # token" without needing a separate wall-clock timer just for flushing.
        return _frame("content_block_stop", {"index": event.index}), True
    if isinstance(event, MessageDelta):
        return _frame(
            "message_delta",
            {"stop_reason": event.stop_reason, "usage": event.usage.model_dump(mode="json")},
        ), False
    return None, False


def _replay_frames(cached: ChatResponse) -> list[str]:
    """Reconstruct a valid (if differently-chunked) SSE sequence from an
    already-persisted ChatResponse — see emit_stream()'s docstring."""
    assistant = cached.assistant_message
    frames = [
        _frame(
            "message_start",
            {
                "user_message_id": cached.user_message.id,
                "assistant_message_id": assistant.id,
                "model": assistant.model,
                # WHY a fresh run_id even on replay: nothing persists the
                # original run_id anywhere to recall it (no cancel endpoint
                # exists yet to have ever needed to look one up — see
                # BUILD_LOG) — it is purely an SSE-frame value.
                "run_id": new_id("run"),
            },
        )
    ]
    for index, block in enumerate(assistant.content):
        start, delta = _replay_block_frames(index, block)
        frames.append(start)
        frames.append(delta)
        frames.append(_frame("content_block_stop", {"index": index}))
    frames.append(
        _frame(
            "message_delta",
            {
                "stop_reason": assistant.stop_reason,
                "usage": assistant.usage.model_dump(mode="json") if assistant.usage else None,
            },
        )
    )
    frames.append(_frame("message_stop", {"status": assistant.status}))
    return frames


def _replay_block_frames(index: int, block: ContentBlock) -> tuple[str, str]:
    # WHY no ImageBlock/ToolResultBlock branch: §3.1 — assistant messages
    # may only contain text, tool_use, reasoning. `cached.assistant_message`
    # is always an assistant-role message, so those two variants can't
    # appear here in practice.
    if isinstance(block, TextBlock):
        start = _frame("content_block_start", {"index": index, "block": {"type": "text"}})
        delta = _frame(
            "content_block_delta",
            {"index": index, "delta": {"type": "text_delta", "text": block.text}},
        )
        return start, delta
    if isinstance(block, ToolUseBlock):
        start = _frame(
            "content_block_start",
            {"index": index, "block": {"type": "tool_use", "id": block.id, "name": block.name}},
        )
        delta = _frame(
            "content_block_delta",
            {
                "index": index,
                "delta": {"type": "input_json_delta", "partial_json": json.dumps(block.input)},
            },
        )
        return start, delta
    if isinstance(block, ReasoningBlock):
        start = _frame("content_block_start", {"index": index, "block": {"type": "reasoning"}})
        delta = _frame(
            "content_block_delta",
            {"index": index, "delta": {"type": "reasoning_delta", "text": block.text}},
        )
        return start, delta
    raise InvalidRequestError(
        f"Cannot replay a persisted {type(block).__name__} in an assistant message.",
        code="chat.unreplayable_block",
    )


async def _run_turn(
    db: AsyncSession,
    user_id: str,
    entry: ModelEntry,
    llm_request: LLMRequest,
    user_row: MessageModel,
    assistant_row: MessageModel,
) -> ChatResponse:
    """Fully consume the adapter's stream, then persist once — the
    non-streaming path's whole reason to exist (see module docstring)."""
    accumulator = _ContentAccumulator()
    terminal: MessageDelta | None = None
    try:
        async for event in _get_adapter(entry).stream(llm_request):
            # WHY reuse _handle_event and discard its frame/flush: the
            # non-streaming path doesn't emit SSE or flush mid-turn, but the
            # event-dispatch logic itself (which accumulator method handles
            # which event type) should have exactly one definition.
            _handle_event(accumulator, event)
            if isinstance(event, MessageDelta):
                terminal = event
    except DomainError:
        assistant_row.content = accumulator.finalize()
        assistant_row.status = "failed"
        assistant_row.completed_at = datetime.now(UTC).replace(tzinfo=None)
        await db.commit()
        raise

    # WHY assert, not a DomainError: the adapter interface (adapter.py)
    # guarantees stream() ends with exactly one MessageDelta if it doesn't
    # raise first — reaching here without one means an adapter violated its
    # own contract, which is a bug in that adapter, not a normal failure
    # mode a caller should have to handle.
    assert terminal is not None, "adapter.stream() ended without a terminal MessageDelta"

    assistant_row.content = accumulator.finalize()
    assistant_row.status = "complete"
    assistant_row.stop_reason = terminal.stop_reason
    assistant_row.usage = _usage_dict(terminal.usage, entry)
    assistant_row.completed_at = datetime.now(UTC).replace(tzinfo=None)
    await users_service.increment_tokens_used(
        db, user_id, terminal.usage.input_tokens + terminal.usage.output_tokens
    )
    await db.commit()
    await db.refresh(assistant_row)

    return ChatResponse(
        user_message=row_to_schema(user_row, include_reasoning=True),
        assistant_message=row_to_schema(assistant_row, include_reasoning=True),
    )


def _usage_dict(usage: LLMUsage, entry: ModelEntry) -> dict[str, Any]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        # WHY None when entry.pricing is None (2026-07-21 update): a model
        # discovered live but absent from the curated catalog has no known
        # per-token price — null means "unpriced," never a fabricated cost.
        # See docs/API_CONTRACT.md §3.3 and core/llm/registry.py's
        # ModelEntry.
        "cost_usd": compute_cost_usd(usage, entry.pricing) if entry.pricing is not None else None,
    }


class _ContentAccumulator:
    """Assembles an adapter's content-block events into final,
    JSONB-storable content blocks.

    WHY tool_use input is buffered separately from the block dict: §3.1 —
    tool arguments stream as JSON string fragments that "the client
    concatenates and parses only after content_block_stop; partial JSON is
    never valid and must not be parsed speculatively." The same rule applies
    to us assembling the persisted block, not just an external client.
    """

    def __init__(self) -> None:
        self._blocks: dict[int, dict[str, Any]] = {}
        self._tool_json_buffers: dict[int, str] = {}
        self._order: list[int] = []

    def start(self, event: ContentBlockStart) -> None:
        self._order.append(event.index)
        block = event.block
        if isinstance(block, TextBlockStart):
            self._blocks[event.index] = {"type": "text", "text": ""}
        elif isinstance(block, ToolUseBlockStart):
            self._blocks[event.index] = {
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": {},
            }
            self._tool_json_buffers[event.index] = ""
        elif isinstance(block, ReasoningBlockStart):
            # WHY signature is always None here: the Anthropic adapter never
            # emits a delta for Anthropic's `signature_delta` (see
            # anthropic_adapter.py's _to_content_block_delta) — and since
            # this slice never requests extended thinking (reasoning_effort
            # is a dropped param, see ADR-0002/BUILD_LOG), Anthropic never
            # actually returns a reasoning block against calls this adapter
            # makes. This branch is presently unreachable in practice, not
            # silently wrong — revisit together with reasoning_effort support.
            self._blocks[event.index] = {
                "type": "reasoning",
                "text": "",
                "redacted": False,
                "signature": None,
            }

    def delta(self, event: ContentBlockDelta) -> None:
        delta = event.delta
        if isinstance(delta, TextDelta):
            self._blocks[event.index]["text"] += delta.text
        elif isinstance(delta, InputJsonDelta):
            self._tool_json_buffers[event.index] += delta.partial_json
        elif isinstance(delta, ReasoningDelta):
            self._blocks[event.index]["text"] += delta.text

    def stop(self, event: ContentBlockStop) -> None:
        block = self._blocks[event.index]
        if block["type"] == "tool_use":
            raw = self._tool_json_buffers[event.index]
            block["input"] = json.loads(raw) if raw else {}

    def finalize(self) -> list[dict[str, Any]]:
        return [self._blocks[i] for i in self._order]


def _get_adapter(entry: ModelEntry) -> ProviderAdapter:
    # WHY ADAPTER_CLASSES lives in core.llm.adapter, not defined here: it's
    # also needed by core.llm.registry.py's live refresh — see that
    # module's own WHY comment for why one shared dict beats two that can
    # drift out of sync.
    factory = ADAPTER_CLASSES.get(entry.provider)
    if factory is None:
        raise InvalidRequestError(
            f"No adapter implemented for provider {entry.provider!r} yet.",
            code="provider.not_implemented",
            details={"provider": entry.provider},
        )
    # WHY getattr, not a per-provider if/elif: every adapter here takes the
    # same __init__(api_key: str) shape, and registry.py's is_available()
    # already relies on this identical `{provider}_api_key` naming
    # convention on app.config.settings — same lookup, not a new one.
    api_key: str | None = getattr(settings, f"{entry.provider}_api_key")
    assert api_key is not None, "checked by registry.is_available() already"
    return factory(api_key)


async def _validate_and_resolve(
    db: AsyncSession, user_id: str, conversation_id: str, data: ChatRequest
) -> tuple[Conversation, ModelEntry, LLMParams]:
    """Everything that can reject a request before any persistence or
    idempotency claim happens. Shared by both response shapes.

    WHY this must run before idempotency claiming: a request that was never
    going to do real work (bad conversation, bad model, unsupported tools,
    usage limit already exceeded) shouldn't claim an Idempotency-Key — only
    requests that actually attempt the turn should be idempotency-tracked.

    WHY a would-be idempotent replay of an already-completed (and
    already-counted) turn is also rejected once a user is over their limit,
    even though replaying it wouldn't consume any new tokens: this check
    runs here, before create_chat_message()/prepare_stream() ever call
    idempotency.check_or_claim() to find out whether the request is a
    replay. Telling the two cases apart would mean resolving idempotency
    before this validation step, inverting the ordering the module
    docstring's own rule depends on. Accepted as an MVP simplification.
    """
    await users_service.check_usage_limit(db, user_id)

    conversation = await conversations_service.get_conversation(db, user_id, conversation_id)

    if data.tools:
        raise InvalidRequestError(
            "Server-side tools are not yet supported.", code="chat.tools_unsupported"
        )

    model_id = data.model or conversation.default_model
    if model_id is None:
        raise InvalidRequestError(
            "No model specified and the conversation has no default_model.",
            code="chat.model_required",
        )
    entry = registry.resolve(model_id)
    if not registry.is_available(entry):
        raise ProviderUnavailableError(
            f"No API key configured for provider {entry.provider!r}.",
            code="provider.not_configured",
            details={"provider": entry.provider},
        )

    params = _resolve_params(conversation, data.params)
    return conversation, entry, params


async def _persist_turn_start(
    db: AsyncSession,
    conversation: Conversation,
    entry: ModelEntry,
    params: LLMParams,
    data: ChatRequest,
) -> tuple[LLMRequest, MessageModel, MessageModel]:
    """Persist the user message and a pending assistant row, and build the
    LLMRequest to send.

    WHY this must only be called once idempotency has confirmed a genuinely
    new attempt: calling it twice for the same logical request would
    persist a duplicate user/assistant message pair.
    """
    history = await _load_history(db, conversation.id)

    user_row = MessageModel(
        id=new_id("msg"),
        conversation_id=conversation.id,
        role="user",
        content=[block.model_dump(mode="json") for block in data.content],
        status="complete",
    )
    db.add(user_row)
    await _bump_conversation(db, conversation.id)
    await db.commit()
    await db.refresh(user_row)

    assistant_row = MessageModel(
        id=new_id("msg"),
        conversation_id=conversation.id,
        role="assistant",
        content=[],
        status="pending",
        model=entry.id,
    )
    db.add(assistant_row)
    await _bump_conversation(db, conversation.id)
    await db.commit()
    await db.refresh(assistant_row)

    llm_request = LLMRequest(
        model=entry.bare_model_id,
        system_prompt=conversation.system_prompt,
        messages=[*history, LLMMessage(role="user", content=data.content)],
        params=params,
    )

    return llm_request, user_row, assistant_row


def _resolve_params(conversation: Conversation, request_params: ChatParams | None) -> LLMParams:
    """Layer system defaults < conversation.default_params < request.params,
    field by field (§5.4: "model and params fall back to the conversation
    defaults when omitted")."""
    defaults = conversation.default_params
    overrides = request_params.model_dump(exclude_unset=True) if request_params is not None else {}

    def pick(field: str, system_default: Any) -> Any:
        if field in overrides and overrides[field] is not None:
            return overrides[field]
        if field in defaults and defaults[field] is not None:
            return defaults[field]
        return system_default

    return LLMParams(
        temperature=pick("temperature", None),
        max_tokens=pick("max_tokens", _DEFAULT_MAX_TOKENS),
        top_p=pick("top_p", None),
        stop_sequences=pick("stop_sequences", []),
        reasoning_effort=pick("reasoning_effort", None),
        response_format=pick("response_format", None),
    )


async def _load_history(db: AsyncSession, conversation_id: str) -> list[LLMMessage]:
    # WHY status IN ("complete", "incomplete"): a "failed" row's partial
    # content was never a coherent turn and may not be valid to replay to a
    # provider (e.g. a dangling unclosed tool_use block); "pending" rows are
    # mid-flight, not history yet. §5.5 explicitly treats "incomplete" as
    # real conversational history, unlike "failed".
    stmt = (
        select(MessageModel)
        .where(
            MessageModel.conversation_id == conversation_id,
            MessageModel.status.in_(("complete", "incomplete")),
        )
        .order_by(MessageModel.id.asc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    # WHY reasoning blocks are NOT stripped here, unlike messages.py's
    # client-facing list (which omits them by default per §3.1): this is
    # history being replayed back to a provider, and §3.1 says the backend
    # must echo reasoning blocks verbatim when a provider requires it — the
    # "omit by default" behavior is specifically about API responses to
    # clients, not our own internal provider calls.
    return [
        LLMMessage(role=row.role, content=_content_block_list_adapter.validate_python(row.content))
        for row in rows
    ]


async def _bump_conversation(db: AsyncSession, conversation_id: str) -> None:
    """Increment message_count for a newly-inserted row; `updated_at` follows.

    WHY this doesn't commit: called once per new message row from within
    `_persist_turn_start`, which commits the message insert and this bump
    together in one transaction. Not called again when a row is later
    updated in place (e.g. assistant pending -> complete) — message_count
    counts rows created, not status transitions.

    WHY no explicit `row.updated_at = ...` here (there used to be one): the
    column already declares `onupdate=func.now()`
    (app/models/conversation.py) — any UPDATE the ORM emits for this row
    already sets `updated_at` from Postgres's own clock. The removed line
    overrode that with `datetime.now(UTC)`, the *app* server's clock, which
    a test comparing this row's `updated_at` before/after against
    Postgres-clock-set values could observe as going backwards whenever the
    two clocks disagreed — see docs/BUILD_LOG.md for the session that found
    this via a reproducing (not flaky) test failure. `conversations.py`'s
    `update_conversation()` never set this column manually either; this was
    the one place inconsistent with that.
    """
    row = (
        await db.execute(select(ConversationModel).where(ConversationModel.id == conversation_id))
    ).scalar_one()
    row.message_count += 1
