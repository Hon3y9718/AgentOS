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
| 6. Remaining providers, tools, files, titling | 🟡 in progress — 4 adapters + live model discovery + Settings-based picker done; gemini adapter, tools, files, titling, cancellation remain |
| 7. Frontend (Streamlit MVP) | ✅ done — real login/signup UI, see auth section below |
| 8. Test coverage (contract fixtures, live smoke) | ⬜ not started |
| 9. Real auth (email/password, JWT) + per-user token limits | ✅ done |

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
9. ~~**Real auth + per-user token limits**~~ Done 2026-07-21 — see BUILD_LOG and
   `docs/DECISIONS/0003 Auth Layering.md`. Pulled forward ahead of items 7/8 on
   direct request, out of the "recommended order"'s original sequencing.

---

## Backend

### Cross-cutting / gaps flagged during exploration
- [x] `docs/DECISIONS/0002 Provider Abstraction.md` — written alongside the Anthropic
      adapter, per plan. Six numbered decisions; read before touching `core/llm/`.
- [x] Real `users` table + FK from `conversations.user_id`/`idempotency_keys.user_id` —
      done 2026-07-21 alongside real auth, see BUILD_LOG and
      `docs/DECISIONS/0003 Auth Layering.md`.
- [ ] Auto-run `alembic upgrade head` on container startup (currently manual).
- [ ] uvicorn's own access/startup logs still aren't JSON (needs a custom `log_config`
      passed to uvicorn — app-level structlog output already is).
- [ ] Reconcile the real `.env` with `.env.example` (old key names, no `DATABASE_URL`) —
      by hand, per BUILD_LOG (Claude is denied `Read` on `.env`).
- [x] `/health/ready` should also check registry load per §5.1 — done, though ADR-0002
      decision 5 notes it's closer to documentation than a live failure detector, since
      the registry loads (and would crash the process) at import time.
- [x] `test_chat.py::test_chat_bumps_message_count_and_updated_at` clock-mixing bug —
      Fixed 2026-07-21, see BUILD_LOG. Was comparing a conversation row's `updated_at`
      set by Postgres's `func.now()` (on creation) against a value `chat.py`'s
      `_bump_conversation()` set from Python's own `datetime.now(UTC)` (on the bump) —
      two different clocks. Reproduced on 3/3 real `make test` runs across two sessions
      (not the one-off VM-clock-drift event first suspected). Fix: stopped
      `_bump_conversation()` from setting `updated_at` at all — the column already
      declares `onupdate=func.now()` (app/models/conversation.py), so any ORM-issued
      UPDATE already sets it from Postgres's own clock; the removed line was overriding
      that with a second, different clock.
- [ ] **Newly discovered:** `main.py` has no exception handler for FastAPI's own
      `RequestValidationError` — only for our `DomainError`. Every 422 caused by FastAPI's
      *own* request validation (a missing required header, an `extra="forbid"` violation,
      a malformed body) currently returns FastAPI's default `{"detail": [...]}` shape, not
      the §2 `{"error": {...}}` envelope. Affects every endpoint, not just chat — found
      while planning the chat endpoint's required `Idempotency-Key` header, deliberately
      not fixed in that slice (cross-cutting, not chat-specific). Needs one handler in
      `main.py`, registered for `RequestValidationError`, mapping to `validation_error`.

### `core/llm`
- [x] `catalog.yaml` + `catalog.py` loader/validator (§4) — Done 2026-07-21, see
      BUILD_LOG. Renamed from `registry.yaml`/most of old `registry.py` — this is now
      the curated *enrichment* table (capabilities/pricing/display data for models
      verified by hand), not the full list of models the API can serve. Still
      crash-loud at import on a malformed row.
- [x] `registry.py` — live model registry (§4). Rewritten 2026-07-21 for live
      discovery: merges `catalog.py`'s curated data with each configured provider's
      own live model list (`ModelRegistry.refresh_if_stale()` — TTL-cached 5min,
      single-flighted, best-effort per provider, never fatal). `resolve()`/
      `is_available()` stay synchronous and network-free (read only the in-memory
      cache) since `chat.py` calls them on every message — see BUILD_LOG for the bug
      this avoided (a live-fetch-gated model would 404 before the first successful
      refresh, if not seeded from the catalog synchronously at construction).
- [x] Normalized request/event types (`types.py`) — reuses `app.schemas.content_block`
      and `StopReason`; does NOT reuse `Usage` (needs registry pricing the service, not
      the adapter, should own — see `LLMUsage`, ADR-0002). `ProviderModel` added
      2026-07-21 for `list_models()` — deliberately no pricing field (only Together
      returns one, as an untrusted float).
- [x] Adapter: anthropic — `stream()` only, no separate non-streaming path (ADR-0002
      decision 3). `reasoning_effort`/`response_format` request params are silently
      dropped (no `X-Params-Dropped` channel exists yet); `file_id` image blocks raise
      (no Files API yet). `list_models()` added 2026-07-21 — the one adapter needing a
      pagination loop (`has_more`/`after_id`), live-verified (10 real models).
- [x] Adapter: openai — wired into `chat.py`'s dispatch in an earlier session;
      `list_models()` added 2026-07-21, live-verified (125 real entries). **Known rough
      edge, not fixed:** OpenAI's `/v1/models` returns every model type the account can
      see, not just chat-capable ones — whisper/tts/embedding models show up in the
      frontend's picker alongside gpt-4o, since nothing in the response distinguishes
      them and this repo has no per-model capability data for anything outside the
      curated catalog. Picking one and sending a message fails with a real (if
      confusing) upstream error rather than crashing — not silently wrong, just not
      filtered.
- [x] Adapter: groq — `list_models()` added 2026-07-21, live-verified (15 real
      entries) — unlike OpenAI's, Groq's response DOES include `context_window` per
      model, passed through.
- [x] Adapter: together — `list_models()` added 2026-07-21, live-verified (273 real
      entries) — its `/v1/models` returns a bare JSON array, unlike every other
      provider's `{"data": [...]}` wrapper.
- [ ] Adapter: gemini — still no adapter, so no `list_models()` for it either; same
      "additive once one adapter proves the abstraction" reasoning as before.
- [ ] Capability enforcement before a provider call (§4) — `chat.py` exists now but
      doesn't do this yet; the registry has the `capabilities` data, nothing calls it.
      Now also has to account for `capabilities` being possibly `None` (a live-only
      model), not just present-or-missing.
- [ ] `X-Params-Dropped` reporting channel — `chat.py` silently drops `reasoning_effort`/
      `response_format` (Anthropic has no equivalent this adapter implements) rather than
      setting this header, since there's still no plumbing from service to router for
      "here's what I dropped." Needed before this is contract-complete.
- [x] `pricing.py` — `compute_cost_usd()`, pure Decimal arithmetic over `LLMUsage` +
      catalog `Pricing`. Gotcha: `Pricing` has no cache-*write* rate (only cache-read) —
      cache writes are billed at the plain input rate, an underestimate. Low-impact today
      (nothing requests prompt caching yet, so cache_write_tokens is always 0 in
      practice) but flagged for whoever adds prompt caching support. Callers must guard
      `pricing is not None` themselves now (2026-07-21) — a live-only model has none.

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

### `app/api/v1` — `conversations.py`, `deps.py`, `models.py` real; `tools.py`/`files.py` don't exist yet
- [x] `GET /api/v1/models` — Done 2026-07-21, see BUILD_LOG. `app/schemas/model.py` +
      `app/services/models.py` are new; no DB session (registry is static/in-memory).
      Query filters (`provider`, `capability` repeated/ANDed, `available`) match §4
      exactly. Now consumed by the frontend's model selector — see Frontend below.
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
- [x] `get_current_user` dependency — real JWT verification via `app.core.auth`, done
      2026-07-21 (was the MVP stub; see BUILD_LOG and ADR-0003).
- [x] `POST /api/v1/auth/register`, `/login`, `/logout` — done 2026-07-21, composes
      fastapi-users' own routers rather than hand-written thin handlers (ADR-0003).
- [ ] Rate-limit headers + `X-Params-Dropped` / `X-Request-Id` wiring (§6)
- [x] **Frontend login/signup UI** — done 2026-07-21, see BUILD_LOG. Was flagged here as
      a gap right after the backend auth slice landed; closed the same day.
- [ ] **Newly discovered:** no self-service way to raise a user's `token_limit` — an
      operator has to update the `users` row directly (no admin endpoint exists).

### Frontend (Streamlit MVP)
- [x] `api_client.py` — the only file allowed to talk HTTP to the backend. Sync `httpx`
      (Streamlit's execution model is sync — no asyncio needed). Config via
      `AGENTOS_API_BASE_URL` env var, not a `config.py` module — that rule is
      `backend/app/`-scoped (`check_layering.sh` only greps that path). Every function
      except `register()`/`login()` takes an explicit `token: str` now (real per-account
      JWTs, done 2026-07-21) — the old shared `AGENTOS_API_TOKEN` env var / module-level
      `_AUTH_HEADERS` constant are gone; see BUILD_LOG and ADR-0003.
      `create_conversation(token, default_model=...)` takes the model as a parameter
      instead of hardcoding it — `DEFAULT_MODEL` is only the initial pre-selection
      before Settings' first `list_models()` call returns. `stream_chat_message(token,
      ..., model=...)` sends the model as a required per-turn override
      (`ChatRequest.model`, §5.4) — nothing PATCHes a conversation's `default_model`
      anymore; the choice is per-turn, not per-conversation.
- [x] Settings page + provider/model selection — Done 2026-07-21, see BUILD_LOG.
      `st.navigation`/`st.Page` (Streamlit 1.36+) splits the app into "Chat" and
      "Settings"; provider (default **groq**, per explicit instruction) and model are
      both chosen only on Settings, as two dependent selectboxes (model options filter
      to the chosen provider, resetting to a sensible default when the provider
      changes). Chat itself stays a plain, bottom-pinned `st.chat_input` with no picker
      at all — a deliberate simplification after finding that `st.chat_input` only
      auto-pins to the viewport bottom when it isn't nested inside a layout container
      like `st.columns`, so an earlier "picker beside the input" design would have
      made the whole composer scroll out of view on a long conversation; asked the
      user how to resolve that tradeoff and they chose "settings only, keep chat
      focused" over either alternative. `st.session_state.selected_model` is still
      session-global (ChatGPT/Claude-style — doesn't resync when switching
      conversations). Each assistant message keeps its small model-name caption
      (`Message.model`, informational only, not a control). Hit and fixed a real
      Streamlit bug along the way: a selectbox using `key="selected_provider"` where
      that same key was *also* written by plain assignment elsewhere silently ignored
      a pre-set session_state value and defaulted to index 0 — fixed by giving every
      such widget its own private key (`_provider_widget`/`_model_widget`) with an
      explicit `index=` and a manual sync back to the real semantic session_state
      variable. Verified live: switching provider on Settings correctly repopulates
      the model list and picks a sensible default; a real send after switching to
      Anthropic surfaced a genuine "credit balance too low" error from Anthropic's own
      API (an account/billing issue, not a bug) — still confirms real routing.
- [x] `app.py` — login/signup screen (tabs, done 2026-07-21) gating everything below it,
      including `st.navigation` itself, until `st.session_state.access_token` is set;
      sidebar conversation list (with delete) + "New conversation" + logout (shown on
      both Chat and Settings pages via a shared `_render_account_sidebar()`), chat
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
- Multi-tenancy hardening (real auth + user-scoping now exist, see item 9 above; social
  login / OAuth backends are the natural next step, fastapi-users supports them without
  a redesign)
- Cost governance and budgets — today's per-user quota (item 9) is a flat lifetime
  counter, not a real budgeting system (no periods, no self-service tiers)
- Provider failover and routing
- Server-side tool execution (as opposed to MVP's client-side `tool_results`)

---

*Last updated: 2026-07-21, after merging live model discovery + Settings-based
provider/model selection with real email/password auth and per-user token limits.*
