# AgentOS API Contract — v1

**Status:** authoritative. This document is written *before* implementation and updated
in the same PR as any behaviour change. If the code and this file disagree, the code is
wrong until this file is amended.

**Audience:** the FastAPI backend, the Streamlit MVP client, the future Next.js client,
and any agent runtime that later drives this API programmatically.

---

## 0. Ground rules

- Base path: `/api/v1`. Breaking changes require `/api/v2`; additive changes do not.
- Content type: `application/json` for everything except streaming (`text/event-stream`)
  and uploads (`multipart/form-data`).
- All timestamps are RFC 3339, UTC, with an explicit `Z` suffix.
- All IDs are opaque strings. Clients must never parse them. Format is a typed prefix
  plus a UUIDv7 — `conv_018f...`, `msg_018f...`, `run_018f...`. UUIDv7 is used so IDs
  sort chronologically, which makes cursor pagination cheap.
- Field naming is `snake_case` in JSON. No camelCase anywhere, including in the Next.js
  era — the TypeScript client maps at the boundary if it wants to.
- Unknown fields in a request are **rejected** (`422`), not ignored. Unknown fields in a
  response must be **ignored** by clients. Servers may add fields at any time.
- Every response body is a defined object. Never a bare array at the top level — that
  blocks adding pagination metadata later.

---

## 1. Authentication

Email/password. A registered account gets a signed JWT access token, sent on every
subsequent request:

```
Authorization: Bearer <jwt>
```

- Every resource is scoped to the resolved user. Fetching another user's conversation
  returns `404`, never `403` — do not leak existence.
- The user's identity is **never** taken from the request body or a query parameter.
- Tokens are stateless and short-lived (1 hour). There is no server-side revocation list —
  `POST /api/v1/auth/logout` is a client-side no-op (discard the token); it does not
  invalidate the token against the server. See `docs/DECISIONS/0003 Auth Layering.md`.

### 1.1 Register / login / logout

`POST /api/v1/auth/register` — JSON body, `application/json` like every other endpoint:

```json
{ "email": "user@example.com", "password": "at least 8 characters" }
```

`201` response:

```json
{
  "id": "user_018f...",
  "email": "user@example.com",
  "is_active": true,
  "is_superuser": false,
  "is_verified": false,
  "token_limit": 1000000,
  "tokens_used": 0
}
```

Errors: `409 conflict` (email already registered), `422 validation_error` (password too
short — currently an 8-character minimum, no other policy).

`POST /api/v1/auth/login` — **`application/x-www-form-urlencoded`, not JSON.** This is
the one endpoint in the API that isn't JSON-in, and it's deliberate: it's the standard
OAuth2 password grant shape (`OAuth2PasswordRequestForm`), not a contract inconsistency.

```
username=user@example.com&password=...
```

(The field is named `username` even though it holds the email — that's the OAuth2 spec's
field name, not this API's choice.)

`200` response:

```json
{ "access_token": "eyJ...", "token_type": "bearer" }
```

Errors: `401 unauthenticated` (wrong email or password).

`POST /api/v1/auth/logout` — requires a valid `Authorization` header. `200` on success;
see the stateless-token caveat above for what this does and does not do.

Every authenticated response includes:

```
X-Request-Id: req_018f...
```

Clients should surface this in error UI. It is the join key across logs, traces, and
provider call records.

### 1.2 Token usage limit

Each account has a flat token quota (`token_limit`, returned on register/`token_limit`/
`tokens_used` fields above). `POST /api/v1/conversations/{id}/messages` (§5.4) checks it
before starting a turn and increments `tokens_used` by `input_tokens + output_tokens` on
completion. Once `tokens_used >= token_limit`, further turns fail with
`402 usage_limit_exceeded` until an operator raises the limit directly (no self-service
upgrade path or periodic reset exists yet).

---

## 2. Error envelope

Every non-2xx response, without exception, has this body:

```json
{
  "error": {
    "type": "rate_limited",
    "message": "Upstream provider rejected the request due to rate limiting.",
    "code": "provider.rate_limited",
    "request_id": "req_018f5c2a...",
    "retryable": true,
    "retry_after_seconds": 12,
    "details": {
      "provider": "groq",
      "model": "llama-3.3-70b-versatile"
    }
  }
}
```

`type` is the stable, client-switchable enum. `message` is human-facing and may change
freely. `details` is free-form and must never be depended on for control flow.

| `type` | HTTP | Retryable | Meaning |
|---|---|---|---|
| `invalid_request` | 400 | no | Malformed or semantically invalid input |
| `validation_error` | 422 | no | Schema validation failed; `details.fields` lists paths |
| `unauthenticated` | 401 | no | Missing or unparseable credentials |
| `permission_denied` | 403 | no | Authenticated but not permitted |
| `not_found` | 404 | no | Resource absent or not visible to this user |
| `conflict` | 409 | no | Idempotency key reuse with a different payload |
| `payload_too_large` | 413 | no | Request or attachment exceeds limits |
| `rate_limited` | 429 | yes | Ours or the upstream provider's limit |
| `context_length_exceeded` | 422 | no | Prompt exceeds the model's context window |
| `content_filtered` | 422 | no | Provider refused on safety grounds |
| `provider_error` | 502 | yes | Upstream returned an error we cannot classify |
| `provider_unavailable` | 503 | yes | Upstream timeout, connection failure, or overload |
| `usage_limit_exceeded` | 402 | no | Per-user token quota exhausted (§1) |
| `internal_error` | 500 | no | Our bug |

Rule: a provider-specific error code must **never** reach the client unmapped. If a new
provider error appears, it lands in `provider_error` with the raw code in `details`, and
that is treated as a bug to be triaged, not a steady state.

---

## 3. Domain model

### 3.1 Content blocks

A message's content is **always an ordered list of typed blocks**, never a string. This
is the single most important shape in the contract. Storing plain strings works fine
until tool calling arrives, at which point every row needs migrating.

```json
{ "type": "text", "text": "What is the weather in Ghaziabad?" }
```

```json
{
  "type": "image",
  "source": { "kind": "base64", "media_type": "image/png", "data": "iVBOR..." }
}
```
`source.kind` may also be `url` (`{"kind": "url", "url": "https://..."}`) or
`file_id` (`{"kind": "file_id", "file_id": "file_018f..."}`).

```json
{
  "type": "tool_use",
  "id": "toolu_018f...",
  "name": "get_weather",
  "input": { "city": "Ghaziabad" }
}
```

```json
{
  "type": "tool_result",
  "tool_use_id": "toolu_018f...",
  "is_error": false,
  "content": [{ "type": "text", "text": "31°C, haze" }]
}
```

```json
{ "type": "reasoning", "text": "...", "redacted": false, "signature": "..." }
```

Reasoning blocks are stored when a provider returns them, but are **omitted from
responses by default**. Pass `?include_reasoning=true` to receive them. Some providers
require reasoning blocks to be echoed back verbatim on the next turn; the backend
handles that internally and clients must not attempt it.

Constraints:
- `assistant` messages may contain `text`, `tool_use`, `reasoning`.
- `user` messages may contain `text`, `image`, `tool_result`.
- `tool_result` blocks must appear before any `text` block in a user message.
- A `system` role does **not** exist as a message. See §3.2.

### 3.2 Conversation

```json
{
  "id": "conv_018f...",
  "title": "Weather questions",
  "system_prompt": "You are a concise assistant.",
  "default_model": "anthropic:claude-sonnet-4-5",
  "default_params": { "temperature": 0.7, "max_tokens": 4096 },
  "metadata": { "source": "streamlit" },
  "message_count": 12,
  "created_at": "2026-07-19T09:14:00Z",
  "updated_at": "2026-07-19T09:41:22Z"
}
```

The system prompt lives on the conversation, not in the message list. Providers disagree
on how system instructions are transmitted (a top-level parameter, a leading message, a
dedicated instruction field), so the API refuses to model it as a message and lets each
adapter place it correctly.

### 3.3 Message

```json
{
  "id": "msg_018f...",
  "conversation_id": "conv_018f...",
  "role": "assistant",
  "content": [{ "type": "text", "text": "It's 31°C and hazy." }],
  "status": "complete",
  "model": "anthropic:claude-sonnet-4-5",
  "stop_reason": "end_turn",
  "usage": {
    "input_tokens": 412,
    "output_tokens": 88,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "reasoning_tokens": 0,
    "cost_usd": "0.002556"
  },
  "created_at": "2026-07-19T09:41:20Z",
  "completed_at": "2026-07-19T09:41:22Z"
}
```

`status`: `pending` | `streaming` | `complete` | `incomplete` | `failed`.
`incomplete` means the stream ended early (client disconnect, cancel, or truncation) but
the partial content was persisted and is valid conversational history.

`stop_reason` is normalized across providers: `end_turn` | `max_tokens` | `tool_use` |
`stop_sequence` | `content_filter` | `error` | `cancelled`.

`cost_usd` is a **decimal string**, not a float. Money never rides a float.

---

## 4. Model registry

Model identifiers are namespaced: `<provider>:<model>`. Providers are
`openai`, `anthropic`, `together`, `groq`, `gemini`.

The namespace exists because model names are not globally unique — the same open-weights
model is served by Together and Groq at different speeds, prices, and context limits, and
routing must be explicit rather than inferred.

### `GET /api/v1/models`

Query: `?capability=tools`, `?capability=vision`, `?provider=groq`, `?available=true`
(repeatable; multiple capabilities are ANDed).

```json
{
  "data": [
    {
      "id": "anthropic:claude-sonnet-4-5",
      "provider": "anthropic",
      "display_name": "Claude Sonnet 4.5",
      "family": "claude",
      "context_window": 200000,
      "max_output_tokens": 64000,
      "capabilities": {
        "streaming": true,
        "tools": true,
        "parallel_tool_calls": true,
        "vision": true,
        "json_mode": true,
        "structured_output": true,
        "reasoning": true,
        "prompt_caching": true,
        "system_prompt": true
      },
      "pricing": {
        "input_per_mtok_usd": "3.00",
        "output_per_mtok_usd": "15.00",
        "cache_read_per_mtok_usd": "0.30"
      },
      "available": true,
      "deprecated_at": null
    }
  ]
}
```

The registry is a **static declarative file** in the repo (`backend/app/core/llm/registry.yaml`),
loaded and validated at startup. It is not fetched from providers at runtime — a provider
outage must not change which models your API claims to support. `available` reflects
whether the required API key is configured plus the last health probe result.

Any request naming a model absent from the registry fails fast with `invalid_request`.
Capability enforcement happens before the provider call: asking a non-vision model for an
image request returns `invalid_request`, not a confusing upstream 400.

---

## 5. Endpoints

### 5.1 Health

`GET /health` — liveness. No auth, no dependencies touched, always `200` if the process
is up. Used by the container orchestrator.

`GET /health/ready` — readiness. Checks DB connectivity and registry load. Returns `503`
with per-check detail if unhealthy. Does **not** call providers.

`GET /api/v1/providers/health` — authenticated, deliberately separate and slow. Performs
a cheap probe per configured provider and reports latency and status. Never used as a
readiness gate.

### 5.2 Conversations

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/conversations` | Create |
| `GET` | `/api/v1/conversations` | List (paginated) |
| `GET` | `/api/v1/conversations/{id}` | Retrieve |
| `PATCH` | `/api/v1/conversations/{id}` | Update title, system prompt, defaults, metadata |
| `DELETE` | `/api/v1/conversations/{id}` | Soft delete |

**Create** request — every field optional:

```json
{
  "title": null,
  "system_prompt": "You are a concise assistant.",
  "default_model": "openai:gpt-4o",
  "default_params": { "temperature": 0.7 },
  "metadata": {}
}
```

If `title` is null it stays null until the first exchange completes, at which point the
backend generates one asynchronously using a cheap model (Groq is the intended default
for this — latency matters more than quality). Clients must handle `title: null` and
render a placeholder; they must not generate titles themselves.

**List** response:

```json
{
  "data": [ { "...conversation..." } ],
  "pagination": { "next_cursor": "eyJpZCI6...", "has_more": true, "limit": 20 }
}
```

Cursor pagination only. No offset/limit — it breaks under concurrent inserts, which is
guaranteed once agents start writing. Params: `?limit=20&cursor=...&order=desc`.

### 5.3 Messages

`GET /api/v1/conversations/{id}/messages`

Query: `?limit=20&cursor=...&order=asc&include_reasoning=false`.

- Cursor pagination works identically to §5.2 (opaque cursor = last-seen message id;
  uuid7 ids sort chronologically, so no separate `created_at` sort key is needed).
- `order` defaults to **`asc`** — chronological, oldest first. This is the opposite
  default from §5.2's conversation list (`desc`), because a transcript reads oldest-first;
  the chat UI always wants this and would otherwise have to pass it on every call.
- `include_reasoning` defaults to `false` (§3.1) — reasoning blocks are stripped from
  each message's `content` list unless set to `true`.

Response — `200`:

```json
{
  "data": [ { "...message (§3.3)..." } ],
  "pagination": { "next_cursor": "msg_018f...", "has_more": true, "limit": 20 }
}
```

Errors: `401 unauthenticated` (missing/malformed Bearer header); `404 not_found` if the
conversation doesn't resolve for this user (absent, soft-deleted, or owned by someone
else — never `403`, per §1).

`DELETE /api/v1/conversations/{id}/messages/{message_id}` — deletes the message **and
every message after it** (ordered by id). This is a truncation operation, not a splice;
leaving a hole in the middle of a transcript produces invalid provider payloads.

Response — `200`:

```json
{ "deleted_message_ids": ["msg_018fa...", "msg_018fb..."], "count": 2 }
```

Errors: `401 unauthenticated`; `404 not_found` if the conversation doesn't resolve for
this user, or if `message_id` doesn't exist in that conversation.

This is how "edit and resend" and "regenerate" are implemented client-side: truncate,
then send a new turn.

### 5.4 Chat — the core endpoint

`POST /api/v1/conversations/{id}/messages`

Headers:
```
Content-Type: application/json
Accept: text/event-stream        # or application/json for non-streaming
Idempotency-Key: <client-generated uuid>
```

Request:

```json
{
  "content": [{ "type": "text", "text": "What's the weather in Ghaziabad?" }],
  "model": "anthropic:claude-sonnet-4-5",
  "params": {
    "temperature": 0.7,
    "max_tokens": 4096,
    "top_p": null,
    "stop_sequences": [],
    "reasoning_effort": null,
    "response_format": null
  },
  "tools": ["get_weather"],
  "tool_choice": "auto",
  "stream": true
}
```

- `model` and `params` fall back to the conversation defaults when omitted.
- `params` is a **normalized** set. Provider-specific knobs are not passed through; if a
  knob matters enough, it gets a normalized name and per-provider translation. Parameters
  a given provider does not support are dropped, and the response includes a
  `X-Params-Dropped: top_k` header rather than failing silently.
- `tools` names tools from the server-side registry. Clients do not send JSON Schema —
  tool definitions are server-owned so that an agent runtime and a chat UI cannot disagree
  about what a tool does.
- `tool_choice`: `auto` | `none` | `required` | `{"name": "get_weather"}`.

**Idempotency.** `Idempotency-Key` is required for this endpoint. The key is stored with
a hash of the request body for 24 hours. Replay with the same body returns the original
result (for streams, a replay of the recorded event sequence). Replay with a *different*
body returns `409 conflict`. This is what makes the retry button safe and what stops an
agent retry loop from duplicating turns.

**Non-streaming response** (`Accept: application/json`) — `201`:

```json
{
  "user_message": { "...message..." },
  "assistant_message": { "...message..." }
}
```

Both messages are returned because the user message ID is not known to the client until
the server assigns it, and the client needs it to render optimistically-sent content with
a real identity.

### 5.5 Streaming (SSE)

`Accept: text/event-stream` on the endpoint above. Response headers:

```
Content-Type: text/event-stream
Cache-Control: no-cache, no-transform
X-Accel-Buffering: no
Connection: keep-alive
```

`X-Accel-Buffering: no` is not optional — without it nginx buffers the whole stream and
your token-by-token UI silently becomes a single blocking response in production.

Every SSE frame carries an explicit `event:` name and a JSON `data:` payload. Do not rely
on the default `message` event.

```
event: message_start
data: {"user_message_id":"msg_018fa...","assistant_message_id":"msg_018fb...","model":"anthropic:claude-sonnet-4-5","run_id":"run_018fc..."}

event: content_block_start
data: {"index":0,"block":{"type":"text","text":""}}

event: content_block_delta
data: {"index":0,"delta":{"type":"text_delta","text":"It's "}}

event: content_block_delta
data: {"index":0,"delta":{"type":"text_delta","text":"31°C"}}

event: content_block_stop
data: {"index":0}

event: content_block_start
data: {"index":1,"block":{"type":"tool_use","id":"toolu_018f...","name":"get_weather","input":{}}}

event: content_block_delta
data: {"index":1,"delta":{"type":"input_json_delta","partial_json":"{\"city\":\"Gha"}}

event: content_block_stop
data: {"index":1}

event: message_delta
data: {"stop_reason":"tool_use","usage":{"input_tokens":412,"output_tokens":88,"cost_usd":"0.001584"}}

event: message_stop
data: {"status":"complete"}
```

Additional events:

```
event: ping
data: {}
```
Emitted every 15 seconds of silence. Keeps proxies and load balancers from killing an
idle connection while a slow model thinks.

```
event: error
data: {"error":{"type":"provider_unavailable","message":"...","retryable":true,"request_id":"req_..."}}
```
Errors that occur *after* headers are sent arrive as an `error` event followed by
`message_stop` with `status: "failed"`. HTTP status is already `200` at that point and
cannot be changed — clients must handle in-stream errors, not just non-2xx responses.
Errors that occur *before* the first byte use the normal error envelope with a real HTTP
status.

**Block indices are contiguous and ordered.** `content_block_delta` for index N never
arrives before `content_block_start` for index N. Tool arguments stream as JSON string
fragments (`input_json_delta`) which the client concatenates and parses only after
`content_block_stop` — partial JSON is never valid and must not be parsed speculatively.

The stream deliberately mirrors Anthropic's event vocabulary, because it is the most
expressive of the five providers. OpenAI, Together, Groq, and Gemini streams are
translated *up* into this shape by their adapters. See `docs/DECISIONS/0002`.

**Cancellation and disconnect.** If the client disconnects, the server aborts the
upstream call, persists whatever content arrived, and marks the assistant message
`incomplete` with `stop_reason: "cancelled"`. Partial content is real history and is sent
on the next turn. An explicit cancel is `POST /api/v1/runs/{run_id}/cancel` → `202`; the
`run_id` comes from `message_start`.

### 5.6 Tools

`GET /api/v1/tools` — lists server-registered tool definitions with their JSON Schema,
so a client can render arguments meaningfully.

`POST /api/v1/conversations/{id}/tool_results` — submits results for pending `tool_use`
blocks when tools are executed client-side. Body is a list of `tool_result` blocks. The
server appends them as a user message and immediately begins the next assistant turn,
returning a stream in the same format as §5.5.

Server-executed tools produce their `tool_result` internally and never surface this
endpoint. Both paths exist because the AgentOS phase needs server-side execution, while
the MVP is simpler with client-side.

### 5.7 Files

`POST /api/v1/files` — `multipart/form-data`, returns `{"id": "file_018f...", "media_type": ..., "size_bytes": ..., "filename": ...}`.
Referenced from content blocks via `{"kind": "file_id", ...}`. Limits: 20 MB per file,
100 MB per request. Exceeding either is `payload_too_large`.

---

## 6. Limits and headers

Rate limit headers on every response:

```
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 57
X-RateLimit-Reset: 1721380800
```

Hard limits, enforced and returned in `details` when exceeded:

| Limit | Value |
|---|---|
| Request body | 25 MB |
| Text block length | 1,000,000 chars |
| Blocks per message | 100 |
| Tools per request | 128 |
| Messages per conversation | 10,000 |
| Stream idle timeout | 120 s |
| Total stream duration | 900 s |

---

## 7. Client obligations

Any client — Streamlit today, Next.js tomorrow, an agent runtime after that — must:

1. Send `Idempotency-Key` on every message creation and reuse it on retry.
2. Handle `error` events mid-stream, not only non-2xx responses.
3. Treat `ping` as a no-op rather than an unrecognized event.
4. Buffer `input_json_delta` fragments and parse only at `content_block_stop`.
5. Render `status: "incomplete"` messages distinctly, not as failures.
6. Ignore unknown block types, event names, and response fields rather than crashing.
7. Fetch `/models` rather than hardcoding a model list.
8. Never construct or parse IDs.
9. Retry `retryable: true` errors with exponential backoff and jitter, honouring
   `retry_after_seconds`. Never retry non-retryable errors.

Point 6 is what lets the backend ship reasoning blocks, new providers, and new event
types without a coordinated frontend release.

---

## 8. Changelog

| Date | Change |
|---|---|
| 2026-07-19 | Initial contract. Conversations, messages, SSE chat, models, tools, files. |
| 2026-07-20 | §5.3 messages: cursor list (`order` default `asc`, `include_reasoning`) and truncate-delete fully specified. |
| 2026-07-20 | Fixed `cost_usd` in §3.3's and §5.5's worked examples (`"0.001584"` → `"0.002556"`) — didn't arithmetically match §4's pricing example for the same model and token counts; doc-internal inconsistency, not a formula change. |
| 2026-07-21 | §1 replaced the MVP auth stub with real email/password auth (register/login/logout, §1.1) and a per-user token usage limit (§1.2, `402 usage_limit_exceeded` added to §2). |