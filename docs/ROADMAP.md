# AgentOS Roadmap

Living checklist of what's built and what's left, for the whole project. Updated at the
end of every work session — check items off, add newly-discovered ones, don't let it
drift from reality.

This file tracks **status only**. It is not where facts live:
- *Why* a decision was made → `docs/BUILD_LOG.md`
- *What* the wire format is → `docs/API_CONTRACT.md`
- *Why* an irreversible choice was made → `docs/DECISIONS/`

If a line here needs more than a checkbox and a task name, that detail belongs in one of
those three, not here.

## How to use this

1. Before starting work, check the next unchecked item in **Recommended order**.
2. When a task completes, check it off here in the same commit/session that finishes it,
   and append the session's narrative to `BUILD_LOG.md` per `CLAUDE.md`.
3. If you discover new scope mid-task, add a line under the relevant section instead of
   letting it live only in your head.

---

## Status snapshot

| Phase | Status |
|---|---|
| 1. Scaffolding (config, health, error taxonomy, telemetry, layering CI) | ✅ done |
| 2. Persistence layer (Conversation + Message tables, schemas, migration) | ✅ done |
| 3. Conversations CRUD (service + router) | ✅ done |
| 4. Provider abstraction (`core/llm`, registry, first adapter) | ✅ done |
| 5. Chat endpoint (the core streamed turn) | ✅ done |
| 6. Remaining providers, tools, files, titling | 🟡 in progress — openai/groq/together adapters wired; gemini + tools/files/titling remain |
| 7. Frontend (Streamlit MVP) | ✅ done |
| 8. Test coverage (contract fixtures, live smoke) | ⬜ not started |

## Recommended order

Reasoning: everything downstream of persistence needs *some* service layer to exist, and
the chat endpoint needs *some* provider adapter to exist — those are the two blocking
paths. Conversations CRUD is pure DB work (no provider dependency), so it's the cheapest
next slice and unblocks the frontend's conversation list early.

1. ~~**Conversations CRUD** — service + router.~~ Done 2026-07-20 — see BUILD_LOG.
2. ~~**Messages** — list with cursor pagination, truncate-delete.~~ Done 2026-07-20 —
   see BUILD_LOG. Per-test DB isolation fixture still not added — still flagged below.
3. ~~**`core/llm` skeleton + one real adapter** (Anthropic).~~ Done 2026-07-20 — see
   BUILD_LOG and `docs/DECISIONS/0002 Provider Abstraction.md`.
4. ~~**Chat endpoint, non-streaming** (`Accept: application/json`)~~ Done 2026-07-20 —
   see BUILD_LOG. Idempotency (§5.4) was pulled forward into this slice rather than
   deferred to item 7 below — see that session's BUILD_LOG entry for why.
4b. ~~**Chat endpoint, SSE** (`Accept: text/event-stream`)~~ Done 2026-07-20 — see
    BUILD_LOG. `ping` keepalive, `message_start`/`message_stop` framing, client-disconnect
    handling (→ `status="incomplete"`), and per-block DB flushing all implemented. The
    explicit `POST /runs/{run_id}/cancel` endpoint stayed deferred to item 7, per plan —
    disconnect handling isn't the same thing as a server-initiated cancel.
5. ~~**Frontend wiring**~~ Done 2026-07-20 — see BUILD_LOG. `api_client.py` +
   a minimal chat UI (`app.py`) against the SSE endpoint.
6. ~~**Remaining provider adapters** (openai, groq, together)~~ Done 2026-07-21 — see
   BUILD_LOG. `gemini` still has no adapter — each was additive once one adapter proved
   the abstraction, per plan.
7. **Tools, files, titling, cancellation** — round out the contract. (Idempotency moved
   to item 4 above — done.)
8. **Test coverage** — contract fixtures per provider, live smoke suite.

---

## Backend

### Cross-cutting / gaps flagged during exploration
- [x] `docs/DECISIONS/0002 Provider Abstraction.md` — written alongside the Anthropic
      adapter, per plan. Six numbered decisions; read before touching `core/llm/`.
- [ ] Real `users` table + FK from `conversations.user_id` (currently unconstrained,
      per BUILD_LOG's persistence-layer session).
- [ ] Auto-run `alembic upgrade head` on container startup (currently manual).
- [ ] uvicorn's own access/startup logs still aren't JSON (needs a custom `log_config`
      passed to uvicorn — app-level structlog output already is).
- [ ] Reconcile the real `.env` with `.env.example` (old key names, no `DATABASE_URL`) —
      by hand, per BUILD_LOG (Claude is denied `Read` on `.env`).
- [x] `/health/ready` should also check registry load per §5.1 — done, though ADR-0002
      decision 5 notes it's closer to documentation than a live failure detector, since
      the registry loads (and would crash the process) at import time.
- [ ] **Newly discovered:** `test_chat.py::test_chat_bumps_message_count_and_updated_at`
      compares a conversation row's `updated_at` set by Postgres's `func.now()` (on
      creation) against a value `chat.py`'s `_bump_conversation()` sets from Python's own
      `datetime.now(UTC)` (on the bump) — two different clocks. Passed in this session's
      own verification run once Postgres was healthy, but is one VM clock-drift event away
      from failing (observed once, immediately after a fresh `colima start`, whose own
      boot log reported a `-323ms` guest-clock adjustment — see BUILD_LOG). Not fixed here
      — unrelated to the adapter-wiring task this session did, and the real fix (use one
      clock, not two) touches `chat.py`'s bump logic, not `core/llm`.
- [ ] **Newly discovered:** `main.py` has no exception handler for FastAPI's own
      `RequestValidationError` — only for our `DomainError`. Every 422 caused by FastAPI's
      *own* request validation (a missing required header, an `extra="forbid"` violation,
      a malformed body) currently returns FastAPI's default `{"detail": [...]}` shape, not
      the §2 `{"error": {...}}` envelope. Affects every endpoint, not just chat — found
      while planning the chat endpoint's required `Idempotency-Key` header, deliberately
      not fixed in that slice (cross-cutting, not chat-specific). Needs one handler in
      `main.py`, registered for `RequestValidationError`, mapping to `validation_error`.

### `core/llm`
- [x] `registry.yaml` + startup loader/validator (§4) — one model (Anthropic's
      claude-sonnet-4-5) for now; add a provider's models only alongside its adapter.
- [x] Normalized request/event types (`types.py`) — reuses `app.schemas.content_block`
      and `StopReason`; does NOT reuse `Usage` (needs registry pricing the service, not
      the adapter, should own — see `LLMUsage`, ADR-0002).
- [x] Adapter: anthropic — `stream()` only, no separate non-streaming path (ADR-0002
      decision 3). `reasoning_effort`/`response_format` request params are silently
      dropped (no `X-Params-Dropped` channel exists yet); `file_id` image blocks raise
      (no Files API yet).
- [x] Adapter: openai — wired into `chat.py`'s `_ADAPTER_CLASSES` this session (see
      BUILD_LOG); the adapter module and its test existed already, only the dispatch +
      registry.yaml entry (`openai:gpt-4o`) were missing.
- [x] Adapter: groq — same as openai above (`groq:llama-3.3-70b-versatile`).
- [x] Adapter: together — same, plus this session wrote `test_together_adapter.py`
      (`together:meta-llama/Llama-3.3-70B-Instruct-Turbo`), which didn't exist yet unlike
      openai/groq's test files — see BUILD_LOG for why that test isn't live-verified the
      way the other three adapters' tests are.
- [ ] Adapter: gemini
- [ ] Capability enforcement before a provider call (§4) — `chat.py` exists now but
      doesn't do this yet; the registry has the `capabilities` data, nothing calls it.
- [ ] `X-Params-Dropped` reporting channel — `chat.py` silently drops `reasoning_effort`/
      `response_format` (Anthropic has no equivalent this adapter implements) rather than
      setting this header, since there's still no plumbing from service to router for
      "here's what I dropped." Needed before this is contract-complete.
- [x] `pricing.py` — `compute_cost_usd()`, pure Decimal arithmetic over `LLMUsage` +
      registry `Pricing`. Gotcha: `Pricing` has no cache-*write* rate (only cache-read) —
      cache writes are billed at the plain input rate, an underestimate. Low-impact today
      (nothing requests prompt caching yet, so cache_write_tokens is always 0 in
      practice) but flagged for whoever adds prompt caching support.

### `app/services`
- [x] `conversations.py` — CRUD + soft delete
- [x] `messages.py` — cursor-paginated list, truncate-delete (`DELETE .../messages/{id}`).
      No `create_message` — that's `chat.py`'s job.
- [x] `chat.py` — the core turn, **both response shapes**. `create_chat_message()`
      (JSON): persist user message, resolve model/params, call `core/llm`,
      accumulate the fully-consumed event stream in memory, persist once.
      `prepare_stream()` + `emit_stream()` (SSE): same setup, but frame events as
      they arrive, flush to DB at each `content_block_stop`, keep the connection
      alive with a `ping` every 15s of provider silence, and detect client
      disconnect (→ persist `status="incomplete"`, `stop_reason="cancelled"`,
      abort the upstream call). `prepare_stream()`/`emit_stream()` are deliberately
      two separate awaitables, not one generator — see BUILD_LOG and the module's
      own docstring for why a `StreamingResponse`-wrapped generator can't produce a
      clean pre-stream HTTP error on its own.
- [x] `idempotency.py` — concurrency-safe (unique-constraint-backed) Idempotency-Key
      store, pulled forward from this list into the chat-endpoint slice (see BUILD_LOG).
      Known imprecision: a genuinely concurrent duplicate request (same key, same body,
      arriving while the first is still `status="pending"`) gets `409 conflict`, not a
      more accurate "retry shortly" signal — §5.4 doesn't define this case explicitly.
- [ ] `titling.py` — async cheap-model title generation (Groq default per §5.2). No
      longer mechanically blocked as of this session's Groq adapter wiring — deferred by
      choice now, not by missing dependency.
- [ ] `tools.py` — server-owned tool registry + `tool_results` turn continuation.
      `chat.py` rejects a non-empty `tools` field for now rather than silently ignoring it.
- [ ] `files.py` — multipart upload, size-limit enforcement
- [ ] Cancellation flag (DB-polled for MVP, per ARCHITECTURE "State and concurrency")

### `app/api/v1` — `conversations.py` and `deps.py` real, rest still empty stubs
- [ ] `GET /api/v1/models`
- [ ] `GET /api/v1/providers/health`
- [x] `POST /api/v1/conversations`, `GET` (list), `GET /{id}`, `PATCH /{id}`,
      `DELETE /{id}`
- [x] `GET /api/v1/conversations/{id}/messages`
- [x] `DELETE /api/v1/conversations/{id}/messages/{message_id}`
- [x] `POST /api/v1/conversations/{id}/messages` — chat, both `Accept` variants.
- [ ] `POST /api/v1/runs/{run_id}/cancel` — deferred (roadmap item 7). Needs its own
      run-tracking table + DB-polled cancellation flag (ARCHITECTURE.md "State and
      concurrency") — a server-*initiated* cancel is a different mechanism from the
      client-*disconnect* handling `chat.py` already does. `run_id` itself exists in
      every `message_start` SSE frame but is never persisted anywhere yet — nothing
      can look one up until this endpoint exists to need it.
- [ ] `GET /api/v1/tools`
- [ ] `POST /api/v1/conversations/{id}/tool_results`
- [ ] `POST /api/v1/files`
- [x] `get_current_user` dependency — MVP stub per §1 (`app/api/v1/deps.py`); real
      token verification is still deferred, only the Bearer-header shape is real
- [ ] Rate-limit headers + `X-Params-Dropped` / `X-Request-Id` wiring (§6)

### Frontend (Streamlit MVP)
- [x] `api_client.py` — the only file allowed to talk HTTP to the backend. Sync `httpx`
      (Streamlit's execution model is sync — no asyncio needed). Config via
      `AGENTOS_API_BASE_URL`/`AGENTOS_API_TOKEN` env vars, not a `config.py` module — that
      rule is `backend/app/`-scoped (`check_layering.sh` only greps that path).
      `create_conversation()` hardcodes `default_model="anthropic:claude-sonnet-4-5"` —
      `GET /api/v1/models` doesn't exist yet, and it's the only model with a real adapter
      anyway. Chat sends go through SSE only, not the non-streaming JSON variant.
- [x] `app.py` — sidebar conversation list (with delete) + "New conversation", chat
      history via `list_messages`, `st.chat_input` → `st.write_stream` fed by parsed SSE
      events, client-side-only title placeholder (`title: null` → "New conversation",
      never persisted or invented server-side, per §5.2/§7). No pagination UI, no
      image/tool_result rendering — neither `files.py` nor `tools.py` exist yet to
      produce those blocks. `st.rerun()` after a send is conditional on success only —
      calling it unconditionally silently discarded the just-rendered `st.error()` before
      a user could read it (found via real browser testing, see BUILD_LOG).
- [x] `docker-compose.yml`'s `streamlit` service sets `AGENTOS_API_BASE_URL: http://api:8000`
      — the client's `localhost:8000` default only works for host-run scripts; inside the
      container it pointed at itself. Same gotcha the `api` service's `DATABASE_URL`
      override already documents; found via real browser testing (`httpx.ConnectError`),
      invisible to the earlier host-run `AppTest` verification by construction.
- [ ] No committed frontend test — verified live via `claude-in-chrome` browser
      interaction plus an earlier one-off `streamlit.testing.v1.AppTest` script, neither
      saved as a real test file. `AppTest` runs app.py's actual script logic headlessly
      without a browser, but doesn't chase `st.rerun()` the way a live session does (see
      BUILD_LOG's "Bug 2") — real browser testing found bugs it missed. Worth a real
      `frontend/streamlit_app/test_app.py` if this UI gets more than trivial further
      changes, but don't treat `AppTest` alone as equivalent to a browser click-through.

### Testing
- [ ] Unit tests for each service with fakes — conversations' tests today are all
      integration-tier (real ASGI app + Postgres); no isolated service-level unit tests yet
- [ ] Contract fixtures — one recorded scenario per provider × (text, tool call, streamed
      tool call, error, truncation). Anthropic partially covered by
      `test_anthropic_adapter.py` (text, tool call, max_tokens truncation, pre-stream and
      mid-stream error) — not yet a "streamed tool call" (tool_use arriving via multiple
      `input_json_delta` fragments across two events, which the test does exercise, but
      not a dedicated streamed-vs-non-streamed distinction since this adapter has only
      one code path, ADR-0002 decision 3).
- [x] Unit tests — `test_pricing.py` is the first (pure function, no DB/network). Every
      other tier is still integration-only.
- [x] Integration tests through the real ASGI app + Postgres — health, conversations,
      messages, and chat, both response shapes (respx-mocked provider) covered; extend
      per new endpoint. Still no per-test DB isolation fixture — a real gotcha hit
      directly in `test_chat.py`'s idempotency tests: a fixed Idempotency-Key literal
      reused across tests (or across separate pytest invocations against the same
      persistent dev Postgres) silently replayed a *different* test's cached response
      instead of exercising its own scenario. Fixed by generating a fresh UUID per test
      call rather than reusing a literal — the same class of problem
      `test_conversations.py`'s exact-ID pagination assertion and `test_messages.py`'s
      cursor tests already worked around by hand: this repo's shared-DB test strategy
      requires every test-owned identifier to be unique, not just distinct-looking.
      `test_chat_stream.py`'s one true socket-disconnect scenario calls
      `chat.prepare_stream()`/`emit_stream()` directly instead of through HTTP —
      `TestClient` can't reliably simulate a mid-stream client disconnect.
- [ ] Live smoke suite — hits all five providers for real, marked, excluded from CI,
      run manually before a release

---

## Later / AgentOS phase — do not start before the MVP above is real

- Multi-step agent loops (built on `core/llm`'s `tool_use` blocks)
- Long-running background runs (the `run_id` machinery already threads through §5.5)
- Multi-tenancy hardening (auth dependency and user-scoping already exist from MVP day 1)
- Cost governance and budgets
- Provider failover and routing
- Server-side tool execution (as opposed to MVP's client-side `tool_results`)

---

*Last updated: 2026-07-21, after wiring the openai/groq/together adapters into `chat.py`'s
dispatch table.*