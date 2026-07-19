# AgentOS Architecture

## What this is

A chat backend that will become an agent runtime. Every decision below is made with the
second half of that sentence in mind: the MVP is a chat app, but nothing in it may assume
that a request is driven by a human clicking a button.

## Layers

```
   HTTP client  (Streamlit MVP → Next.js later → agent runtime eventually)
        │  JSON / SSE only
   ─────┼──────────────────────────────────────────────────────────────
        ▼
   app/api/v1/        routers · auth deps · SSE framing · HTTP status mapping
        ▼
   app/services/      business logic · orchestration · transactions
        ▼                    │
   app/models/  app/db/      ▼
   persistence         app/core/llm/    provider adapters · normalized event stream
                             ▼
                       OpenAI · Anthropic · Together · Groq · Gemini
```

**Dependencies point downward only.** `services` must not import from `api`. `core/llm`
must not import from `services`. There is no circularity anywhere and no clever registry
that reintroduces it.

### The two load-bearing rules

**1. No `fastapi` import under `app/services/`.**
Enforced in CI by a grep. The reason is not purity — it is that the agent runtime, the
title-generation background job, and the future CLI all need to call the same logic
without an HTTP request existing. Services raise domain errors from `app/core/errors.py`;
a single exception handler in `app/api/` maps those to the §2 error envelope.

**2. `frontend/` never imports `backend/`.**
Also enforced in CI. Streamlit is a disposable client that happens to be written in
Python. The moment it imports a service, the Next.js migration becomes a rewrite instead
of a swap. All frontend HTTP access goes through `frontend/streamlit_app/api_client.py`
and nowhere else.

## Package responsibilities

| Package | Owns | Must not |
|---|---|---|
| `api/v1/` | Routing, auth deps, status codes, SSE framing | Contain business logic |
| `schemas/` | Wire format in and out | Touch the DB or providers |
| `services/` | Orchestration, transactions, decisions | Import `fastapi` |
| `models/` | SQLAlchemy tables | Contain business methods |
| `core/llm/` | Provider adapters, normalized events, registry | Know about conversations |
| `core/errors.py` | The domain error taxonomy | Import HTTP anything |
| `core/telemetry/` | structlog config, request IDs, tracing | Be optional |
| `db/` | Session lifecycle, migration entry point | Contain queries |

`core/llm/` deliberately knows nothing about conversations, users, or persistence. It
takes a normalized request and yields normalized events. That boundary is what makes it
testable without a database and reusable by the agent loop.

## Request lifecycle for a streamed chat turn

1. Middleware assigns a request ID and binds it to the structlog context.
2. Router resolves the user, validates the body, checks the idempotency key.
3. Service persists the user message and creates the assistant message as `pending`.
4. Service loads history, resolves the model from the registry, and validates that the
   requested capabilities exist on that model.
5. Service asks `core/llm` for an event stream and yields normalized events upward.
6. Router frames those events as SSE. It performs no translation beyond serialization.
7. As deltas arrive, the service accumulates content blocks in memory and flushes to the
   DB periodically, not per token.
8. On terminal event: persist final content, `stop_reason`, usage, and computed cost;
   mark `complete`.
9. On disconnect or cancel: abort upstream, persist what arrived, mark `incomplete` with
   `stop_reason: "cancelled"`.
10. On upstream error: emit an `error` event, mark `failed`, persist partial content.

Step 9 is the one people skip and regret. Partial assistant content is legitimate
conversational history and must survive.

## State and concurrency

- The API is stateless. Any process can serve any request. No in-memory session store,
  no sticky sessions — that is what makes horizontal scaling and the eventual agent
  worker pool possible.
- Cancellation of an in-flight run on a *different* process requires shared state. MVP
  uses a DB-polled cancellation flag; this moves to Redis pub/sub when it becomes a
  bottleneck, and the `POST /runs/{id}/cancel` contract does not change either way.
- One transaction per request by default. Streaming is the exception: the user message
  commits before the stream opens, and assistant content is committed incrementally.

## Configuration

Everything through `app/config.py` using pydantic-settings. `os.getenv` appears nowhere
else in the codebase — enforced by grep in CI. Missing required configuration fails at
startup, loudly, not at first use. Provider API keys are optional individually; a
provider with no key configured is marked `available: false` in the registry rather than
crashing the app.

## Observability

structlog, JSON output, request ID bound to every line. Every provider call emits one
structured record: provider, model, latency, token counts, cost, stop reason, retry
count, outcome. That record is the raw material for the cost dashboard and for debugging
agent loops later.

Never logged: API keys, bearer tokens, message content, tool arguments, system prompts.
Log lengths and hashes instead. This is not paranoia — it is the difference between an
incident and a breach.

## Testing

- **Unit** — services with fake providers and an in-memory or transactional DB. Fast.
- **Contract** — one recorded fixture per provider per scenario (text, tool call,
  streamed tool call, error, truncation). These catch adapter regressions without
  spending money or requiring network.
- **Integration** — through the real ASGI app against a real Postgres in Docker.
- **Live smoke** — a tiny suite that hits all five providers for real. Marked, excluded
  from CI by default, run manually before a release. Providers change behaviour without
  notice; this is how you find out on your terms.

## Path to AgentOS

The MVP is shaped so these are additive, not structural:

| Later capability | Enabled by |
|---|---|
| Multi-step agent loops | `core/llm` returning normalized `tool_use` blocks; services already handle a tool turn |
| Long-running background runs | The `run_id` in `message_start` and stateless request handling |
| Multi-tenancy | The `get_current_user` dependency and user-scoped queries present from day one |
| Cost governance and budgets | Per-call cost records written from the first commit |
| Provider failover and routing | The registry plus a uniform adapter interface |
| Server-side tool execution | `POST /tool_results` and the server-owned tool registry |

None of these require moving a layer. That is the whole point of the constraints above.