# ADR-0002: Provider Abstraction

**Status:** accepted, 2026-07-20.

**Context:** `ARCHITECTURE.md` requires `core/llm/` to know nothing about
conversations, users, or persistence, and to be callable without an HTTP
request existing. `API_CONTRACT.md` §5.5 defines one normalized SSE event
vocabulary, deliberately modeled on Anthropic's, that every provider's stream
must translate into. §4 requires a static, declarative model registry loaded
at startup, never probed from providers at runtime. This ADR settles the
concrete shape of that abstraction, decided while building the first real
adapter (Anthropic) rather than in the abstract, per the roadmap's own
reasoning for sequencing it that way.

---

## Decision 1: raw `httpx`, not a per-provider SDK

Every adapter calls its provider over `httpx.AsyncClient`, not the vendor's
official Python SDK.

**Alternatives considered:** each adapter using its provider's official SDK
(`anthropic`, `openai`, etc.). `CLAUDE.md`'s wording — "All LLM calls go
through `app/core/llm/`. Never import a provider SDK elsewhere" — reads as
SDKs being the intended shape, walled off one per adapter.

**Why `httpx` won anyway:** one HTTP library across all five eventual
adapters means one place to set the timeout policy (`CLAUDE.md`: "every
external call has an explicit timeout"), one retry/backoff implementation,
and no divergent conventions between five SDKs that don't agree on how they
surface streaming, timeouts, or error types. The cost is real: each adapter
hand-rolls request signing and parses provider SSE frames itself instead of
getting that from an SDK. Accepted because the alternative — normalizing
five SDKs' different retry/timeout/error conventions into one behavior — is
at least as much code, just spent per-provider instead of once.

**Consequence:** `app/core/llm/anthropic_adapter.py` builds Anthropic's
`x-api-key` / `anthropic-version` headers and parses `text/event-stream`
lines by hand. The next adapter (OpenAI, etc.) does the same against its own
wire format — there is no shared "provider SDK" layer to lean on, only
shared `httpx` client configuration.

---

## Decision 2: normalized content blocks reuse `app.schemas.content_block`

`core/llm/types.py`'s `LLMMessage.content` is typed as
`list[app.schemas.content_block.ContentBlock]` — the same §3.1 union the API
layer validates request/response bodies against — not a parallel set of
block types defined inside `core/llm/`.

**Why this doesn't violate the layering rule:** `ARCHITECTURE.md`'s
dependency diagram is `api/v1 → services → (models/db, core/llm)`.
`schemas/` isn't drawn in that diagram at all — it has no dependency on
`services/`, `models/`, `db/`, or `core/llm/` (confirmed: nothing under
`app/schemas/` imports any of them). That makes it a leaf package, same as
`core/llm/`, not a rung above it. "`core/llm` knows nothing about
conversations" is about conversation/user/persistence concepts — a content
block (text, image, tool_use, tool_result, reasoning) isn't one of those; it
is exactly what a provider adapter needs to produce and consume.

**Alternative considered:** a fully separate block-type hierarchy inside
`core/llm/`, decoupled from the wire format, on the theory that `core/llm/`
should be extractable as a standalone package with zero knowledge of
anything under `app/schemas/`. Rejected: the realistic failure mode of two
definitions of "what a tool_use block looks like" is silent drift between
what the API accepts and what an adapter actually emits — worse than the
coupling this avoids, since nothing would catch that drift until a specific
provider's tool-call response failed to round-trip.

**Consequence:** `core/llm/` now has a real import-time dependency on
`app/schemas/`. Future readers should not infer from "`core/llm` knows
nothing about conversations" that it may not import `app/schemas/` — it may,
for wire-shape types only (`content_block.py`, and `message.py`'s
`StopReason` literal), never for anything conversation- or persistence-
shaped (`Message`, `Conversation`).

**One exception, deliberately not reused: `app.schemas.message.Usage`.**
That type requires `cost_usd`, a decimal computed from token counts × the
registry's per-model pricing — and `ARCHITECTURE.md`'s request lifecycle
assigns "computed cost" to the *service*, not the adapter. `core/llm/` has
no business reaching into pricing data to price its own output. So
`core/llm/types.py` defines its own `LLMUsage` (raw token counts only, no
`cost_usd`) for `MessageDelta`; the service combines `LLMUsage` with a
resolved `RegistryEntry.pricing` to build the wire `Usage` when it persists
or returns the message.

---

## Decision 3: one adapter method, always streaming internally

`ProviderAdapter` has exactly one method: `stream(request) -> AsyncIterator[LLMEvent]`.
There is no separate non-streaming `complete()`. Every provider call is made
in streaming mode, even to serve the `Accept: application/json` case (§5.4)
— the future chat service fully consumes the stream and returns one JSON
body instead of forwarding SSE frames, rather than the adapter making a
different kind of request.

**Why:** `ARCHITECTURE.md`'s request lifecycle already describes a single
abstraction — "service asks `core/llm` for an event stream and yields
normalized events upward" (singular: *an* event stream, not two kinds).
`ROADMAP.md`'s plan to build the chat endpoint "non-streaming first, then
SSE" is about the endpoint's response framing being easier to get right
first, not evidence that the adapter needs two request modes. A `stream()` +
`complete()` split roughly doubles adapter code — once now, times five once
every provider exists — for a distinction (chunked vs. buffered HTTP
response) that belongs entirely to the response-framing layer `api/v1/`
already owns.

**Consequence:** a non-streaming client request still pays streaming
transport overhead against the provider. Accepted: model inference latency
dominates total request time; the difference between a chunked and
non-chunked HTTP response to Anthropic is noise by comparison.

---

## Decision 4: adapters raise from `app.core.errors` directly

Each adapter catches everything provider-specific — HTTP status codes,
connection failures, malformed responses — and re-raises a `DomainError`
subclass from §2's taxonomy before anything leaves `core/llm/`. Nothing
above this package ever sees a raw `httpx` exception or a provider's own
error code.

**Why not a second, `core/llm`-local error type that services then
translate:** `app/core/errors.py`'s taxonomy already has no `fastapi`
dependency and is not conceptually service-specific — its docstring says
"Called by: `app/services/*`" because that's the only caller that exists
yet, not because it's forbidden elsewhere. Introducing a parallel
`core/llm`-only error hierarchy would mean a second mapping table (provider
error → `core/llm` error → §2 type) doing the same job the first one
already does, with more places for a provider error code to leak through
unmapped — exactly what §2's own rule warns against.

**Consequence:** `app/core/errors.py` is now imported from two packages,
not one. Its rule stands unchanged: it must never import `fastapi` or
anything HTTP-shaped, from either caller.

---

## Decision 5: the registry loads eagerly, at import time

`core/llm/registry.py` parses and validates `registry.yaml` at module import
— `registry = _load_registry()` at module scope — the same
crash-loudly-before-binding-a-port pattern `app/config.py` uses for
`Settings()`. A malformed `registry.yaml` is treated as an operator error
caught at boot, not a per-request failure mode.

**Consequence for §5.1's readiness check:** because the registry can only
ever be in a "loaded successfully" state by the time `/health/ready` runs
(a bad file would have crashed the process before it could serve any
request), that check is closer to documentation — confirming the contract's
stated behavior — than to a live failure detector, unlike the database
check next to it, which really can fail after a successful boot.

---

## Decision 6: `ping` is not a `core/llm` concern

The normalized `LLMEvent` union has no `Ping` variant. §5.5's `event: ping`
(emitted every 15s of stream silence, to keep proxies from killing an idle
connection) is a stream-transport/keepalive concern belonging to whichever
layer owns SSE framing — `api/v1/`, per `ARCHITECTURE.md`'s package table —
once the chat endpoint exists. Making every adapter implement its own idle
timer would mean five duplicate implementations of the same 15-second clock
for a concern that has nothing to do with any specific provider.

**Consequence:** this slice does not implement `ping` at all (there is no
chat endpoint yet to own it). The event union is shaped now so that when the
chat endpoint lands, no adapter needs to change to accommodate it — the
timer wraps the adapter's iterator from outside, not from within any
`stream()` implementation.

**Update, 2026-07-20 (SSE slice):** implemented in
`app/services/chat.py`'s `emit_stream()`, confirming the shape predicted
above — no adapter changed. The implementation surfaced a real,
non-obvious pitfall worth recording: the naive approach,
`asyncio.wait_for(agen.__anext__(), timeout=15)` retried in a loop, is
**broken**. `wait_for()` cancels its wrapped coroutine on timeout, and
cancelling an async generator's in-flight `__anext__()` permanently
exhausts it — verified empirically (a throwaway script) before writing
this: the *next* call after a timeout raises `StopAsyncIteration`
immediately, indistinguishable from a clean end-of-stream, silently
dropping the rest of the provider's response. The fix is
`asyncio.wait({task}, timeout=15)` around a task that's created once and
reused across repeated timeouts — `wait()`, unlike `wait_for()`, leaves a
timed-out task alive and still running in the background, so re-waiting on
the *same* task lets it eventually complete with the real event. Same
empirical verification applies to disconnect cleanup: cancelling that
pending task and `await`ing it (to let the cancellation actually settle)
before calling `agen.aclose()` runs the adapter's `async with
httpx.AsyncClient(...)` cleanup correctly; calling `aclose()` immediately
after `.cancel()` without awaiting the cancellation first risks a "generator
already running" error. Any future adapter or streaming code doing anything
similar (racing an async generator against a timeout) should reuse this
pattern, not rediscover the `wait_for()` pitfall independently.

---

## What this ADR deliberately leaves open

Capability enforcement before a provider call (§4: "asking a non-vision
model for an image request returns `invalid_request`, not a confusing
upstream 400") is not implemented in this slice. The registry's
`capabilities` field makes that check possible, but nothing calls it yet —
it belongs to `app/services/chat.py`, the next roadmap item, which is the
first thing that will actually have a request and a resolved model in the
same place at the same time.
