# Build Log

Append-only. Newest entry at the bottom, dated. See CLAUDE.md's Learning mode.

---

## 2026-07-20 — Scaffolding session

**Built:** the whole non-business-logic skeleton — nothing here calls a provider,
touches a real table, or serves a real endpoint beyond health.

- `backend/pyproject.toml` — uv project, strict ruff (`E,F,I,UP,B,SIM,C4`) + strict
  mypy, prod deps (fastapi, uvicorn, pydantic-settings, sqlalchemy[asyncio],
  asyncpg, alembic, structlog) and dev deps (ruff, mypy, pytest, pytest-asyncio,
  httpx).
- `app/config.py` — `Settings` (pydantic-settings), instantiated at *module import
  time* so a missing `DATABASE_URL` crashes on `import app.config`, not on the
  first request that happens to read it.
- `app/core/errors.py` — the full §2 taxonomy as `DomainError` subclasses. Each
  subclass carries `type`/`http_status`/`retryable` as class attributes, so a
  service can't construct a mismatched pairing.
- `app/core/telemetry/` — `logging.py` (structlog → JSON), `middleware.py`
  (`RequestIDMiddleware`, binds `request_id` to both structlog's contextvars and
  `request.state`).
- `app/db/base.py` + `session.py` — declarative `Base`, async engine with
  `pool_pre_ping=True`, `get_db()` generator dependency.
- `app/main.py` — lifespan (configure logging on startup, dispose the engine on
  shutdown), the one `DomainError` → JSON exception handler, `/health`,
  `/health/ready`.
- `backend/Dockerfile` — multi-stage (uv builder → slim runtime), non-root
  `appuser`.
- `docker-compose.yml` — postgres:16 with a `pg_isready` healthcheck and a named
  volume; `api` depends on that healthcheck; `streamlit` has no Dockerfile yet
  (installs at container start — fine, it's disposable per CLAUDE.md).
- `backend/alembic/` — initialized, `env.py` pulls the URL from `app.config` and
  `target_metadata` from `app.db.base.Base`, runs migrations through the async
  engine (`asyncio.run` + `run_sync`, since asyncpg has no sync mode). No
  migrations exist — nothing in `app/models/` yet to autogenerate from.
- `scripts/check_layering.sh` — three greps: no `fastapi` under
  `backend/app/services/`, no `backend` import under `frontend/`, no
  `os.getenv`/`os.environ` outside `backend/app/config.py`.
- `.github/workflows/ci.yml` — `lint`, `layering`, `test` jobs. `test` runs a
  real `postgres:16` service container, because `test_health_ready` hits a real
  DB rather than a mock (ARCHITECTURE.md's "Integration" tier).
- `.env.example` — rewritten for the five providers named in CLAUDE.md
  (openai/anthropic/together/groq/gemini) + `DATABASE_URL` + `LOG_LEVEL`, one
  comment per var. The old file (Langfuse keys, a misspelled `CLAUD_API_KEY`)
  didn't match anything the code now reads.
- `tests/` — `conftest.py` seeds a default `DATABASE_URL` before importing
  `app.main` (since `Settings()` runs at import time), `test_health.py`,
  `test_health_ready.py`.

**Verified for real, not just written:** `ruff check`, `ruff format --check`,
`mypy app` (strict), `./scripts/check_layering.sh`, `pytest` against a live
`docker compose up postgres`, `alembic current` connecting successfully,
`docker compose build api` + `docker compose up api` + `curl` against both
health endpoints on the actual running container.

### Decisions

- **`app/schema/` → `app/schemas/`.** The pre-scaffolded dir was singular;
  ARCHITECTURE.md's package table says `schemas/`. Renamed to match the doc,
  per your call.
- **Health endpoints live in `app/main.py`, not `app/api/v1/health.py`.** §0
  scopes `/api/v1` to the versioned API; §5.1's `/health` and `/health/ready`
  are explicitly outside that path. The three pre-existing empty stub files
  under `api/v1/` (`chat.py`, `conversations.py`, `health.py`) were left alone,
  per your call — still empty, still out of scope.
- **`/health/ready` checks DB connectivity only**, not "registry load" as §5.1
  also specifies. There is no registry yet (`core/llm/` is an empty scaffold).
  Left a `# TODO` pointing at §5.1 in `main.py` — pick this back up once
  `registry.yaml` exists.
- **Request IDs use `uuid4`, not the UUIDv7 the contract specifies for
  resource IDs** (`conv_`/`msg_`/`run_`). Python 3.12's stdlib `uuid` has no
  `uuid7` (lands in 3.14); adding a third-party uuid7 package wasn't worth it
  for an ID that doesn't need the sortability that motivates v7 for paginated
  resources. Revisit if a uuid7 dependency gets pulled in anyway once
  conversation/message IDs are implemented — then request IDs might as well
  match.
- **`readiness` returns `"unreachable"` to the client, not the raw exception
  string**, even though `/health/ready` is unauthenticated. A raw asyncpg
  error can contain hostnames or auth-failure detail; the full string still
  goes to the structured log for whoever's watching it.
- **mypy needed `explicit_package_bases = true` + `mypy_path = "."`.** With no
  `__init__.py` anywhere (this repo uses PEP 420 namespace packages
  throughout, matching the pre-existing empty dirs), mypy otherwise registers
  `app/config.py` as both `config` and `app.config` and refuses to proceed.

### Understand before the next step

- **The error-handling contract**: any future service raises from
  `app/core/errors.py`; only `main.py`'s `domain_error_handler` is allowed to
  know that maps to JSON+HTTP. Don't catch `DomainError` anywhere else.
- **`get_db()` is a generator dependency, not a plain return**, specifically so
  FastAPI keeps the session open through a streaming (SSE) response — a plain
  `return session` would close it as soon as the handler function returns,
  before a stream starts sending.
- **`Settings()` fails at import time.** Anything that imports `app.config`
  (directly or transitively) now requires `DATABASE_URL` to be set. Tests set
  a default in `conftest.py` before importing `app.main` — mirror that pattern
  if you add another entrypoint.
- **`docker-compose.yml`'s `api` service overrides `DATABASE_URL`** even
  though `env_file: .env` is also set — inside the compose network the
  hostname is `postgres`, not `localhost`. `.env`'s copy of `DATABASE_URL` is
  for host-run tools (pytest, alembic).

### Deliberately deferred (scope was scaffolding only)

- All LLM code, `core/llm/registry.yaml`, provider adapters.
- Every endpoint except `/health` and `/health/ready`.
- Actual tables in `app/models/` and the first real Alembic migration.
- Frontend (`frontend/streamlit_app/app.py` is still empty — the `streamlit`
  compose service will render a blank page until that exists).
- Making uvicorn's own access/startup log lines JSON. They come from loggers
  with `propagate=False` in uvicorn's own logging config, so `configure_logging()`
  doesn't reach them — only app code calling `structlog.get_logger()` is JSON
  today. Fixing this needs a custom `log_config` passed to uvicorn itself.
- The real `.env` file still has the old key names (`OPEN_AI_API_KEY`,
  `CLAUD_API_KEY`, no `DATABASE_URL`) — I didn't touch it since your updated
  `.claude/settings.json` denies `Read` on it, by design. You'll want to bring
  it in line with the new `.env.example` by hand.

---

## 2026-07-20 — Persistence layer (Conversation + Message)

**Built:** the first real tables, their wire schemas, and the first Alembic
migration. Still no service or router code — this is data layer only, the
next natural slice after the scaffolding session above.

- `app/core/ids.py` — `new_id(prefix)`, generating `conv_<uuid7hex>`-style IDs.
  Added the `uuid6` dependency for this (asked before adding it, per CLAUDE.md).
- `app/models/conversation.py`, `app/models/message.py` — SQLAlchemy tables
  matching API_CONTRACT §3.2/§3.3. `content`/`usage`/`default_params`/`metadata`
  are JSONB columns, not normalized tables or Postgres ENUMs.
- `app/schemas/content_block.py` — the §3.1 content-block union
  (text/image/tool_use/tool_result/reasoning), discriminated on `type`.
- `app/schemas/conversation.py`, `app/schemas/message.py` — read/create/update
  shapes mirroring the contract field-for-field, `extra="forbid"` throughout.
- `backend/alembic/versions/75a4f46f4297_add_conversations_and_messages.py` —
  autogenerated, reviewed, applied. Downgrade tested round-trip.

**Verified for real:** round-tripped a sample assistant message (text +
tool_use blocks) through `Message.model_validate` and confirmed
`ConversationCreate` rejects an unknown field with a real `ValidationError`.
Ran `ruff`, `ruff format --check`, `mypy app` (strict, 16 files), the layering
script, and the full `pytest` suite (4 tests) against a live
`docker compose up postgres`. Downgraded the migration to base and re-upgraded
to confirm both directions work, then inspected the live tables with `psql \dt`.

### Decisions

- **`metadata_` (Python attribute) vs `metadata` (DB column, wire field).**
  SQLAlchemy's `DeclarativeBase` already owns a class-level `.metadata` (the
  `MetaData` registry) — naming an instance column `metadata` the obvious way
  would collide with it. Used `mapped_column("metadata", ...)` to keep the
  Python name `metadata_` while the actual column and JSON field stay
  `metadata`. **This means an ORM row can't be turned into the
  `Conversation` read schema via plain `model_validate(obj, from_attributes=True)`**
  — `from_attributes` looks up `obj.metadata`, which resolves to SQLAlchemy's
  registry, not the JSONB dict. Whoever writes `app/services/conversations.py`
  next has to map that field by hand (`metadata=row.metadata_`). Called out in
  both files' module headers so it isn't a surprise later.
- **JSONB, not normalized tables, for `content`/`usage`/`default_params`.**
  Nothing needs to query *inside* a content block yet (e.g. "find all messages
  with a tool_use for X"). If that need shows up, it's a later migration, not
  a rethink of this one — the Pydantic schema is already the real shape
  authority, the column is just where it's stored.
- **No Postgres ENUM for `role`/`status`/`stop_reason`.** A DB enum needs a
  migration to add a value; providers add new stop reasons over time. Plain
  `String` columns, validated by the Pydantic `Literal` types at the API
  boundary instead.
- **`user_id` has no ForeignKey.** There's no users table yet — auth is still
  the API_CONTRACT §1 stub. The column exists and is indexed now (so
  user-scoped queries are correct from day one, per ARCHITECTURE.md) but the
  FK constraint arrives with real auth.
- **Enabled alembic's `post_write_hooks` (ruff check --fix, then ruff format)**
  in `alembic.ini`. The autogenerated migration didn't match this project's
  ruff config out of the box (old `Union[...]` syntax, unsorted imports, a
  too-long line) and CI's lint job covers `alembic/versions/` too. This makes
  every future `make migrate` self-fixing — the tool corrects its own output,
  which isn't the same thing as CLAUDE.md's "never hand-edit
  `alembic/versions/*`" (that's about not rewriting migration *logic* by hand).

### Understand before the next step

- **Building a response body for a conversation is not a one-liner.** Whoever
  writes the conversations service needs to construct
  `schemas.Conversation(..., metadata=row.metadata_, ...)` explicitly — see
  the `metadata_`/`metadata` decision above.
- **`new_id("conv")` is the only correct way to generate a primary key.**
  Nothing in the model layer generates IDs (models have no business logic);
  the service layer calls `new_id()` before constructing the ORM object.
- **The migration file is real and committed, but not self-applying.** A
  fresh postgres volume (e.g. after `docker compose down -v`) starts with no
  tables until something runs `alembic upgrade head` against it — nothing
  does that automatically on container startup yet. Note this only replays
  the *existing* migration; `make migrate m="..."` is for generating a *new*
  one after a model change, not for applying what's already there.

### Deliberately deferred

- Running migrations automatically on container startup (currently manual,
  via `make migrate`).
- `app/services/conversations.py` and `app/api/v1/conversations.py` — CRUD is
  the next slice.
- A real `users` table and the FK from `conversations.user_id`.
- Everything from the scaffolding session's deferred list still stands (LLM
  code, registry, chat endpoint, frontend app code, uvicorn JSON logs).

---

## 2026-07-20 — Conversations CRUD (service + router)

**Built:** the five §5.2 endpoints end to end — `POST/GET/PATCH/DELETE
/api/v1/conversations[/{id}]` — plus the first real auth dependency and the
first cursor-paginated list response. This is the first slice with an actual
HTTP surface a client can call.

- `app/api/v1/deps.py` — `get_current_user`, the MVP stub from §1: requires a
  `Bearer <token>` header (401 if missing/malformed), token value itself
  never checked, always resolves to a fixed `DEV_USER_ID`. Also exports
  `CurrentUser = Annotated[str, Depends(get_current_user)]`.
- `app/db/session.py` — added `DbSession = Annotated[AsyncSession,
  Depends(get_db)]` alongside `get_db`, for the same reason as `CurrentUser`
  above (see the ruff decision below).
- `app/schemas/pagination.py` — `Pagination` + `ConversationList`, matching
  §5.2's `{"data": [...], "pagination": {...}}` list shape.
- `app/services/conversations.py` — `create_conversation`, `list_conversations`,
  `get_conversation`, `update_conversation`, `delete_conversation`. No
  `fastapi` import (verified by `check_layering.sh`).
- `app/api/v1/conversations.py` — the router, wired into `main.py` under
  `/api/v1`.
- `backend/tests/test_conversations.py` — 9 new integration tests through the
  real ASGI app + Postgres; `backend/tests/conftest.py` gained a shared
  `auth_headers` fixture.

**Verified for real:** full `ruff check`/`ruff format --check`/`mypy app`
(strict) clean; `check_layering.sh` clean; `pytest` green (13/13) against a
freshly-migrated Postgres (`docker compose down -v` then re-up, to rule out
state left over from the persistence-layer session). Then `make dev`'s `api`
service, built and run for real: curled create → list → get → patch (null
clearing `system_prompt`) → delete (204) → get-after-delete (404) →
unauthenticated (401), all through the actual container, not just tests.

### Decisions

- **`ruff`'s B008 rule flags `Depends(get_db)` in an argument default but,
  inconsistently, not the first `Depends(...)` in the same signature** (only
  ever flagged the second dependency parameter — never fully diagnosed why).
  Rather than sprinkle `# noqa: B008`, switched to FastAPI's `Annotated`
  dependency style everywhere: `CurrentUser` and `DbSession` type aliases
  (defined next to `get_current_user` and `get_db` respectively) replace
  `= Depends(...)` defaults at every call site. This is also just the
  current FastAPI-recommended idiom, independent of the ruff quirk.
  **Gotcha this creates:** parameters using these aliases carry no Python-level
  default, so in a signature that mixes them with `Query(default=...)`
  params, the `Annotated` ones must come *first* — Python's own rule that a
  non-default parameter can't follow a default one still applies. See
  `list_conversations` in `app/api/v1/conversations.py`.
- **`deleted_at = datetime.now(UTC)` fails against Postgres.** Every
  timestamp column here is `TIMESTAMP WITHOUT TIME ZONE` (the SQLAlchemy
  default for a plain `Mapped[datetime]`), and asyncpg rejects a tz-aware
  Python value for a naive column at bind time
  (`can't subtract offset-naive and offset-aware datetimes`). Fixed with
  `.replace(tzinfo=None)` after computing the UTC value — the timestamp is
  still correct UTC, just stripped of the marker. This only surfaced when a
  DELETE was actually exercised against real Postgres; sqlite or a mock
  session wouldn't have caught it. If a timezone-aware column is ever wanted
  later, that's a deliberate `DateTime(timezone=True)` migration, not a
  workaround in the service layer.
- **Cross-event-loop asyncpg crash when a test both drives `TestClient` and
  opens its own `async with async_session_factory()`.** `TestClient` runs the
  ASGI app through an anyio portal in its own event loop; the shared
  `app.db.session.engine`'s connection pool becomes bound to whichever loop
  first used it. A test coroutine awaiting the shared session factory
  directly (to set up an "other user's" conversation, bypassing the API since
  the auth stub can't actually authenticate a second user) opened a
  connection in pytest-asyncio's loop instead, and returning it to the pool
  broke the next checkout from `TestClient`'s loop
  (`Future ... attached to a different loop`). Fixed by giving that one test
  helper (`_create_conversation_for_user` in `test_conversations.py`) its own
  throwaway engine, created and disposed inside a single `asyncio.run()` —
  it never touches the shared pool. No production code path does this; it's
  a test-only gotcha, but worth knowing before writing the next test that
  needs to seed data outside the API.
- **Pagination test asserts exact IDs, not just counts.** The DB has no
  per-test isolation fixture (still doesn't — flagged again below), so
  earlier tests' conversations for the same dev user are still in the table
  when the pagination test runs. Asserting `has_more is False` after two
  pages assumed exactly 3 rows existed, which only held by accident of test
  ordering. Rewrote to assert the *exact* IDs expected on each page, relying
  on IDs being chronologically sortable (`core/ids.py`) rather than on total
  row count — correct regardless of what other tests leave behind.
- **PATCH uses `ConversationUpdate.model_dump(exclude_unset=True)`**, not a
  filter on `is not None`. `None` is a legitimate value for `system_prompt`
  etc. (clears it); only `exclude_unset` distinguishes "field omitted" from
  "field explicitly set to null."
- **`limit` capped at `Query(default=20, ge=1, le=100)`** on the list
  endpoint. §6 documents no hard ceiling for this particular endpoint (unlike
  the message/tool/file limits it does specify) — this is an implementation
  choice, not a contract limit, called out as such in the code rather than
  added to `API_CONTRACT.md`.

### Understand before the next step

- **Every future authenticated router imports `CurrentUser` from
  `app/api/v1/deps.py` and `DbSession` from `app/db/session.py`** — don't
  reintroduce inline `Depends(...)` defaults; you'll hit the same ruff
  friction this session did.
- **There is still no per-test DB isolation fixture.** Tests share one
  Postgres database and never truncate it. This has been fine so far because
  tests either use unique generated IDs or (per the pagination decision
  above) assert relative/exact expectations instead of absolute counts. The
  next resource with more complex cross-resource state (messages, which
  depend on conversations existing) should probably introduce a real
  transactional-rollback fixture rather than keep working around it by hand.
- **`message_count` on the `Conversation` schema is always `0`.** Nothing
  increments it yet — that's the messages/chat slice's job, not this one's.

### Deliberately deferred

- Rate-limit headers / `X-RateLimit-*` (§6) — cross-cutting across every
  endpoint, not specific to conversations; belongs to its own slice.
- Real token verification — `get_current_user`'s fake resolution is
  untouched; only the shape (`Bearer` header → user ID) is real, per §1.
- Everything from the earlier sessions' deferred lists that this slice didn't
  touch: LLM code/registry, the chat endpoint itself, `messages.py`,
  `tools.py`, `files.py`, frontend app code, uvicorn JSON logs, auto-run
  migrations on container startup, a real `users` table/FK.

---

## 2026-07-20 — Messages (list + truncate-delete)

**Built:** the two §5.3 endpoints — `GET /api/v1/conversations/{id}/messages`
(cursor-paginated) and `DELETE /api/v1/conversations/{id}/messages/{message_id}`
(truncate: deletes that message and everything after it). No message
*creation* here — that's the chat endpoint's job (roadmap item 4, still
unbuilt), so this slice is read + truncate only.

- `docs/API_CONTRACT.md` §5.3 — fleshed out from a one-paragraph sketch into
  a full spec: request/response shapes, the `order` default, error types.
  Added a §8 changelog row.
- `app/schemas/pagination.py` — `MessageList`, alongside the existing
  `ConversationList` (same `Pagination` wrapper, different item type).
- `app/schemas/message.py` — `MessageDeleteResult` (`deleted_message_ids`,
  `count`), the truncate-delete response shape.
- `app/services/messages.py` — `list_messages`, `delete_message_and_after`.
  No `fastapi` import (verified by `check_layering.sh`).
- `app/api/v1/messages.py` — the router, nested at
  `/conversations/{conversation_id}/messages`, wired into `main.py`.
- `backend/tests/test_messages.py` — 13 new integration tests through the
  real ASGI app + Postgres.

**Verified for real:** `ruff check`/`ruff format --check` clean; `mypy app`
(strict) clean once four speculative `# type: ignore` comments were removed
(mypy flagged them as unused — pydantic's own validation handles the
`str` → `Literal[...]` coercion at the schema boundary, no cast needed);
`check_layering.sh` clean; `pytest` green (26/26, up from 13) against a fresh
`docker compose up postgres` + `alembic upgrade head`. Then ran the API for
real (`uvicorn` against the same Postgres, not through Docker this session)
and curled: create conversation → empty list → unauthenticated (401) →
unknown conversation (404) → delete-nonexistent-message (404) — then seeded
three real message rows via `psql` and curled: default order (confirmed
`asc`, chronological), `?order=desc`, `?limit=2` cursor pagination (confirmed
`next_cursor`/`has_more`), reasoning block omitted by default and included
with `?include_reasoning=true`, and the truncate-delete itself (deleting the
2nd of 3 messages correctly removed it and the 3rd, left the 1st).

### Decisions

- **§5.3's contract text was ambiguous** on whether `order=asc` was the
  endpoint's actual default or just what the chat UI happens to pass. Asked
  before assuming (skill instruction: "a wrong assumption baked into a
  contract is expensive to remove") — confirmed **`asc` is the default**,
  the opposite of `desc` for §5.2's conversation list. Reasoning: a
  transcript reads oldest-first; making the UI pass `?order=asc` on every
  single call would be pure friction. Written into the contract explicitly
  now so the next reader doesn't hit the same ambiguity.
- **Frontend `api_client.py` deliberately not touched this slice**, even
  though the add-endpoint skill's default template says to add the method.
  Asked and confirmed: `ROADMAP.md`'s recommended order puts all frontend
  wiring at step 5, after the chat endpoint exists — there's nothing worth
  building a UI against yet. Revisit once step 5 starts.
- **No `create_message` service function added**, even for test convenience.
  Tests seed `Message` rows directly via the ORM (`_seed_messages` in
  `test_messages.py`), reusing the same throwaway-engine-in-`asyncio.run()`
  pattern `test_conversations.py` established for `_create_conversation_for_user`
  (same reason: the shared `app.db.session` engine is bound to `TestClient`'s
  event loop). Adding a real `create_message` now would be scope creep ahead
  of `chat.py`, which needs to own it alongside `message_count` bookkeeping
  (see next point).
- **`conversation.message_count` is *not* touched by the truncate-delete**,
  even though deleting messages should shrink it. Nothing increments it yet
  (still always `0`, per the conversations-slice BUILD_LOG entry) — writing
  decrement-only logic against a counter no code path increments yet would
  be dead logic exercised by a test asserting nothing meaningful. `chat.py`
  should own increment *and* decrement together so the invariant is
  established in one place, not split across two unrelated PRs.
- **Truncate-delete is a hard SQL `DELETE`, not a soft delete.** Unlike
  `Conversation`, `Message` has no `deleted_at` column (see
  `app/models/message.py`) — the contract doesn't treat a truncated message
  as recoverable history the way a soft-deleted conversation is.
- **Ownership/404 scoping is enforced by calling
  `conversations_service.get_conversation(db, user_id, conversation_id)`**
  at the top of both `list_messages` and `delete_message_and_after`, rather
  than re-deriving the `user_id` + `deleted_at IS NULL` filter here. Reuses
  already-tested logic; the returned `Conversation` schema is discarded —
  only the "raises `NotFoundError`" side effect matters. The minor cost is
  one extra query per call; not worth optimizing away at this scale.
- **Reasoning-block filtering happens in Python after the fetch, not in the
  SQL query.** `content` is a JSONB list (no normalized column to filter on
  per-block), so `_to_schema` drops `type == "reasoning"` entries from the
  already-fetched list before handing it to the `Message` schema — mirrors
  how `conversations._to_schema` already hand-maps `metadata_` → `metadata`.

### Understand before the next step

- **`app/services/messages.py` importing `app/services/conversations.py` is
  intentional, not a layering violation** — `ARCHITECTURE.md` forbids
  `services` importing `api`, not services importing each other. Any future
  service needing "does this conversation belong to this user" should call
  `conversations_service.get_conversation`, not re-implement the query.
- **The message list endpoint's default order (`asc`) differs from the
  conversation list's (`desc`)** — don't copy-paste `list_conversations`'s
  router signature without checking the default value if writing a third
  paginated list endpoint later.
- **There is still no way to create a message through the API.** Anyone
  writing a test or a manual smoke check against `/messages` needs to seed
  rows directly (see `_seed_messages` in `test_messages.py`) until `chat.py`
  exists.

### Deliberately deferred

- `frontend/streamlit_app/api_client.py` — no message-list/delete method
  added this slice (see Decisions above); bundled with the rest of frontend
  wiring at roadmap step 5.
- `conversation.message_count`/`updated_at` bookkeeping on delete — bundled
  into `chat.py`'s eventual increment/decrement logic (see Decisions above).
- Everything from earlier sessions' deferred lists that this slice didn't
  touch: LLM code/registry, the chat endpoint itself (`chat.py`, message
  *creation*), `tools.py`, `files.py`, rate-limit headers, real token
  verification, auto-run migrations on container startup, a real `users`
  table/FK, per-test DB isolation fixture.

---

## 2026-07-20 — core/llm skeleton + Anthropic adapter

**Built:** the first code under `core/llm/` — normalized request/event
types, the `ProviderAdapter` interface, the static model registry (§4), and
a real Anthropic adapter translating its Messages API into the normalized
event vocabulary §5.5 was modeled on. Also wrote `docs/DECISIONS/0002
Provider Abstraction.md` for real (it had been an empty file cited as
authoritative since the scaffolding session). No wiring into a live request
path yet — `chat.py` and the chat endpoint are still empty stubs; this slice
is adapter-only, reachable so far only from tests and a manual `/health/ready`
curl.

- `app/core/llm/types.py` — `LLMRequest`/`LLMMessage`/`LLMParams`/
  `ToolDefinition` (input) and `ContentBlockStart`/`ContentBlockDelta`/
  `ContentBlockStop`/`MessageDelta` (output, `LLMEvent` union). Reuses
  `app.schemas.content_block.ContentBlock` and `app.schemas.message.StopReason`;
  defines its own `LLMUsage` (raw token counts, no `cost_usd`) rather than
  reusing `app.schemas.message.Usage` — see Decisions.
- `app/core/llm/adapter.py` — `ProviderAdapter` Protocol, one method:
  `stream(request) -> AsyncIterator[LLMEvent]`.
- `app/core/llm/registry.yaml` + `registry.py` — one entry
  (`anthropic:claude-sonnet-4-5`, pricing/limits copied verbatim from
  API_CONTRACT §4's own worked example), loaded and validated at import
  time. `ModelRegistry.resolve()` raises `InvalidRequestError` for an
  unknown model; `.is_available()` computes from `settings.<provider>_api_key`.
- `app/core/llm/anthropic_adapter.py` — the real adapter. Builds Anthropic
  Messages API requests over raw `httpx` (not the `anthropic` SDK), parses
  its SSE stream, translates all five §3.1 content-block types both
  directions (with two explicit non-goals — see Decisions), maps Anthropic's
  ~7 error types onto §2's taxonomy, maps its stop reasons onto §3.3's.
- `app/main.py` — `/health/ready` now checks the registry too, closing the
  `# TODO` that had sat in this file since the scaffolding session.
  Consequence noted in ADR-0002 decision 5: since the registry loads at
  import time, this check can't fail without the whole process already
  having failed to start — it's closer to confirming documented behavior
  than to a live failure detector, unlike the database check next to it.
- `backend/pyproject.toml` — three new dependencies: `httpx` (promoted from
  dev-only to prod), `pyyaml` (was already resolved transitively via
  `uvicorn[standard]`, now declared directly since `registry.py` imports it),
  `respx` + `types-pyyaml` (dev-only, for mocking `httpx` in tests and typing
  `pyyaml` under strict mypy).
- `backend/tests/test_registry.py`, `test_anthropic_adapter.py` — 9 new
  tests. The adapter tests are contract-tier (ARCHITECTURE.md): fixture SSE
  bodies through a `respx`-mocked transport, no real network. Cover a
  text response, a tool-call response (including multi-fragment
  `input_json_delta`), a `max_tokens` truncation, a pre-stream 429 with
  `retry-after` parsing, and a mid-stream `error` event arriving after
  partial content.
- `backend/tests/test_health_ready.py` — one new test asserting
  `checks["registry"] == "ok"`.

**Verified for real:** `ruff check`/`ruff format --check` clean; `mypy app`
(strict, 25 files) clean; `check_layering.sh` clean; `pytest` green (36/36,
up from 26) against `docker compose up postgres`. Then ran the API for real
(`uvicorn` against the same Postgres) and curled `/health/ready` — confirmed
`checks.registry == "ok"` outside of pytest, not just inside it. Could not
do a live network smoke test against the real Anthropic API — no
`ANTHROPIC_API_KEY` is configured in this environment (checked via
`settings.anthropic_api_key is not None` without reading `.env`, which
Claude is denied `Read` on by design). Flagged as deferred, not skipped
silently — do this by hand once a key is available, before this adapter is
trusted for anything beyond the mocked test suite.

### Decisions

Six numbered decisions live in `docs/DECISIONS/0002 Provider Abstraction.md`
in full; summarized here, don't duplicate the reasoning — read the ADR for
the "why," especially before adding the next adapter:

1. **Raw `httpx`, not a provider SDK per adapter** — asked and confirmed.
   One HTTP client, one timeout/retry policy across all five eventual
   adapters, at the cost of hand-rolling request signing and SSE parsing
   per provider instead of getting it from an SDK.
2. **Normalized content blocks reuse `app.schemas.content_block`** — asked
   and confirmed. `schemas/` isn't in `ARCHITECTURE.md`'s dependency diagram
   at all; it's a leaf package like `core/llm/`, not a rung above it, so
   this doesn't invert the forbidden `api → services → core/llm` direction.
   The one thing NOT reused from `schemas/` is `Usage` — see below.
3. **One adapter method, `stream()` only** — asked and confirmed. Every
   provider call streams internally, even for a non-streaming client
   request; the future chat service buffers a fully-consumed stream into
   JSON when it needs to, rather than every adapter implementing two
   request modes.
4. **Adapters raise `app.core.errors.DomainError` subclasses directly**, not
   a second `core/llm`-local error type the service then translates.
   `core/errors.py`'s docstring says "Called by: app/services/*" only
   because that was true when it was written, not because it's a hard
   boundary — it has no `fastapi` dependency either way.
5. **`registry.yaml` loads eagerly, at import time** — same
   crash-loudly-before-binding-a-port pattern as `app.config.settings`. A
   malformed file is an operator error caught at boot, not a per-request
   condition. Direct consequence: the `/health/ready` registry check (item
   above) can't really fail on a running process.
6. **`ping` (§5.5's 15s keepalive) is not in the `LLMEvent` union at all** —
   it's a stream-transport concern for whichever layer frames SSE
   (`api/v1/`, once `chat.py` exists), not something every adapter should
   independently implement a timer for.

Beyond the ADR's six:

- **`MessageDelta.usage` is `LLMUsage` (raw counts), not
  `app.schemas.message.Usage`.** `Usage.cost_usd` is a required decimal
  computed from token counts × registry pricing — `ARCHITECTURE.md`'s
  request lifecycle assigns "computed cost" to the service, not the
  adapter. This is a correction I made mid-session: the ADR's first draft
  of decision 2 said `Usage` was reused wholesale; caught the `cost_usd`
  mismatch before writing the adapter and fixed both the ADR and
  `types.py` together rather than shipping the wrong shape and discovering
  it when `chat.py` tries to persist a `Usage` with a fabricated cost.
- **Adapter file is named `anthropic_adapter.py`, not `anthropic.py`.**
  Python 3 doesn't do implicit relative imports, so `import httpx` inside
  a module literally named `anthropic.py` wouldn't actually collide with
  the third-party `anthropic` package — but naming it identically to a
  well-known PyPI package one directory below is a needless readability
  trap for zero benefit, since nothing requires the filename to match.
- **`reasoning_effort` and `response_format` request params are silently
  dropped** by the adapter, not translated. Anthropic has no
  `response_format` equivalent at all; `reasoning_effort` could map to its
  "extended thinking" `budget_tokens` param, but only via an invented
  low/medium/high → token-count heuristic, plus handling Anthropic's
  constraint that thinking mode forces `temperature=1`. Real scope, not
  rushed into this slice. §5.4 says a dropped param should surface as an
  `X-Params-Dropped` response header, but `core/llm/`'s `stream()` only
  yields `LLMEvent`s — there's no channel back to the router for "here's
  what I dropped" yet. Not invented speculatively; wait for `chat.py` to
  need it.
- **`file_id` image blocks raise `InvalidRequestError`, not silently
  drop or best-effort translate.** They reference our own Files API
  (§5.7), which doesn't exist yet — there's nothing to resolve a
  `file_id` into bytes with. Raising is honest; guessing would silently
  send garbage to Anthropic.
- **Unrecognized Anthropic stream event types are ignored, not fatal**
  (mirrors §7's client-obligation "ignore unknown event names," applied to
  this adapter reading Anthropic's own stream). Unrecognized *block* or
  *delta* types inside a recognized event, however, raise `ProviderError`
  — those aren't forward-compatible additions we can safely no-op on, since
  we'd be silently dropping actual message content.
- **A mid-stream Anthropic `error` event is signaled by raising** from
  inside the `async for` in `stream()`, not by a normalized error variant
  in `LLMEvent` (there isn't one). The caller tells "nothing came through"
  from "partial content came through, then it broke" by whether it
  received any events before the exception propagated — exercised directly
  in `test_stream_raises_on_mid_stream_error_event_after_partial_content`.
- **Pricing/context-window/max-tokens figures in `registry.yaml` are
  copied verbatim from API_CONTRACT §4's own worked example**, not
  independently re-verified against Anthropic's live pricing page. Said so
  in a comment in the YAML file itself — money data that feeds real
  billing later shouldn't carry unstated provenance.

### Understand before the next step

- **`chat.py` is the first thing that will actually call `AnthropicAdapter.stream()`
  for real.** It needs to: resolve `provider:model` via `registry.resolve()`,
  strip the provider prefix before constructing `LLMRequest` (adapters see
  bare model names), combine the yielded `LLMUsage` with
  `RegistryEntry.pricing` to compute `cost_usd`, and own the 15s `ping`
  timer and the 900s total-duration cap — neither exists anywhere yet.
- **Capability enforcement (§4: reject an image request against a
  non-vision model before calling the provider) has nowhere to live until
  `chat.py` exists.** The registry has the `capabilities` data; nothing
  calls it yet.
- **Adding the next adapter (openai) means adding its models to
  `registry.yaml` at the same time**, not before — per `core/llm/README.md`,
  untested registry data for a provider with no adapter isn't worth
  carrying.

### Deliberately deferred

- A live smoke test against the real Anthropic API — no key configured in
  this environment; do this by hand before trusting the adapter beyond its
  mocked test suite (see Verified for real, above).
- `X-Params-Dropped` header wiring, and actually supporting
  `reasoning_effort`/`response_format` for Anthropic — both need `chat.py`.
- Capability enforcement before a provider call (§4) — needs `chat.py`.
- Connection pooling / a shared `httpx.AsyncClient` with a real lifecycle
  (currently a fresh client per `stream()` call) — needs a lifespan owner,
  which doesn't exist until something wires this adapter into `main.py` for
  real.
- Everything else already on the roadmap: the four remaining provider
  adapters, `chat.py` itself, `tools.py`, `files.py`, `titling.py`,
  idempotency, cancellation, rate-limit headers, real token verification,
  a real `users` table/FK, per-test DB isolation fixture.

---

## 2026-07-20 — Chat endpoint (non-streaming)

**Built:** `POST /api/v1/conversations/{id}/messages`, `Accept: application/json`
only (§5.4) — the endpoint where conversations, messages, and `core/llm`
finally meet. Idempotency-Key handling (§5.4) was pulled forward into this
slice rather than left for roadmap item 7, after you confirmed that call —
built concurrency-safe (a real DB unique constraint, not just a
sequential-retry check), since §5.4 frames it as the thing that stops an
agent retry loop from duplicating turns, and that's exactly a concurrency
property, not just a sequential one.

- `app/schemas/chat.py` — `ChatParams`/`ChatRequest`/`ChatResponse`, §5.4
  field-for-field. `stream` is accepted but inert; dispatch is on the
  `Accept` header, not the body.
- `app/schemas/content_block.py` — added `Field(max_length=1_000_000)` to
  `TextBlock.text` (§6's text-block-length limit) — this is the first
  endpoint accepting client-submitted content, so the first place this limit
  actually needed enforcing. `ChatRequest.content` similarly got
  `Field(min_length=1, max_length=100)` for §6's blocks-per-message limit.
- `app/models/idempotency_key.py` + migration `2e74c9417fe0` — `key` (the
  raw client header value) is the primary key; its own DB-level uniqueness
  *is* the concurrency-safety mechanism, not application-level locking.
- `app/services/idempotency.py` — `check_or_claim()`/`complete()`/`abandon()`.
  Insert-and-catch-`IntegrityError` to detect a concurrent claim; lazy 24h
  TTL expiry (checked at lookup time, no scheduled sweep — none of that
  infra exists); cross-user key collision resolved as the same `409
  conflict` a body mismatch gets, not a distinguishable error (§1: never
  leak existence, applied to idempotency lookups too, not just resource
  fetches).
- `app/core/llm/registry.py` — added `RegistryEntry.bare_model_id` (strips
  the `"provider:"` prefix ADR-0002 already said adapters expect).
- `app/core/llm/pricing.py` — `compute_cost_usd(usage, pricing) -> str`,
  `Decimal` throughout. Placement corrected `core/llm/README.md`'s own
  earlier "must never live here" line for cost computation — see Decisions.
- `app/services/chat.py` — the orchestration: scope-check the conversation,
  reject unsupported `tools`, resolve model (request → conversation default
  → `invalid_request` if neither), resolve params (system default <
  conversation default < request override, field by field), claim the
  idempotency key, persist the user message + bump `message_count`, persist
  the assistant message as `pending`, load history, call the adapter,
  accumulate its event stream in memory via a small `_ContentAccumulator`,
  persist the assistant message once (`complete` or `failed` — see
  Decisions on why no `incomplete` here), compute cost, record the
  idempotency result.
- `app/api/v1/chat.py` — the router. Rejects `Accept: text/event-stream`
  cleanly rather than attempting it; `Idempotency-Key` is a required
  `Header()` (422 if missing, via FastAPI's own validation — see the
  newly-discovered gap below).
- `app/services/messages.py` — `_to_schema` renamed to `row_to_schema`
  (dropped the `_` prefix) so `chat.py` can reuse the identical row→wire
  mapping instead of duplicating it.
- `backend/pyproject.toml` — no new prod dependency this slice (`httpx`,
  `pyyaml` already added for `core/llm`).
- `backend/tests/test_pricing.py` — 5 new unit tests (no DB, no network —
  the first tests in this repo at that tier; every prior test has been
  integration-tier).
- `backend/tests/test_chat.py` — 14 new integration tests, respx-mocked
  Anthropic transport through the full ASGI app (same technique as
  `test_anthropic_adapter.py`, now exercised via `TestClient` instead of
  calling the adapter directly).

**Verified for real:** `ruff check`/`ruff format --check` clean; `mypy app`
(strict, 30 files) clean; `check_layering.sh` clean; migration
downgrade→upgrade round-trip confirmed; `pytest` green (55/55, up from 36)
against `docker compose up postgres`, run twice in a row to confirm
repeatability against the shared dev DB (not just luck from a clean state).
Then ran the API for real and curled: missing `Idempotency-Key` → 422;
`Accept: text/event-stream` → clean `invalid_request` (not a hang or a
confusing 500); unknown model → `invalid_request`; non-empty `tools` →
`invalid_request`; no `ANTHROPIC_API_KEY` configured → `provider_unavailable`
with `retryable: true`. Then queried `idempotency_keys` directly via `psql`
and confirmed all five of those curled failures left **zero** rows — every
validation-only rejection happens before the idempotency key is ever
claimed, exactly as designed. Could not curl the real happy path (no
Anthropic key in this environment, same limitation as the `core/llm`
session) — covered by `test_chat.py`'s respx-mocked happy-path test instead.

### Decisions

- **Idempotency built now, concurrency-safe** — asked and confirmed, per
  the tension between `ROADMAP.md`'s sequencing (idempotency as a later,
  separate item) and §5.4's contract text (required, with real
  concurrent-duplicate-prevention semantics). Built as a real DB unique
  constraint on `key`, not a sequential-retry-only check, because that's
  the actual property §5.4 calls out as the point of the feature. One
  known imprecision, documented in code and `ROADMAP.md`: a genuinely
  concurrent duplicate (same key, same body, arriving while the first is
  still `pending`) gets `409 conflict`, which undersells it — retrying once
  the first call finishes is exactly correct, and `409`'s `retryable:
  false` says the opposite. Not inventing a new §2 error type to fit this
  one case more precisely.
- **Failed messages count and list normally** — asked and confirmed. A
  `status="failed"` assistant message still increments
  `conversation.message_count` and appears in `GET .../messages`; hiding it
  anywhere would make the count lie relative to what the list endpoint
  actually returns. It's still excluded from what gets sent back to the
  *provider* as history (see below) — those are different questions with
  different answers, not one rule applied inconsistently.
- **Found and fixed a real inconsistency in `API_CONTRACT.md` itself**,
  not in the implementation: §3.3's and §5.5's worked examples both show
  `cost_usd: "0.001584"` for `anthropic:claude-sonnet-4-5` at
  `input_tokens: 412, output_tokens: 88` — but §4's pricing example for
  that exact model (`$3.00`/`$15.00` per Mtok) arithmetically produces
  `0.002556` for those counts, not `0.001584`. Caught this by writing
  `compute_cost_usd()` against the standard "tokens × price ÷ 1e6" formula
  and testing it against the contract's own worked numbers before wiring
  it into `chat.py` — the two illustrative examples were written
  independently and never cross-checked against each other. Fixed both
  occurrences in `API_CONTRACT.md` to the arithmetically correct value and
  added a §8 changelog row, rather than either silently matching my
  formula to a wrong number or leaving the authoritative doc self-
  inconsistent.
- **`pricing.py` lives in `core/llm/`, correcting `core/llm/README.md`'s
  own prior "must never live here" line for cost computation.** That rule
  came from the adapter session and was really about an *adapter* never
  reaching into registry pricing itself (why `MessageDelta.usage` is
  `LLMUsage`, not the wire `Usage`, per ADR-0002). A pure function over
  this package's own types (`LLMUsage`, `registry.Pricing`) is a different
  thing — putting it here once means the future SSE chat service reuses
  the identical formula instead of a second copy drifting into existence.
  Corrected the README in place with an explicit note explaining the
  narrower real rule, rather than silently contradicting what I'd
  documented in the previous session.
- **`Pricing` has no cache-*write* rate.** `compute_cost_usd()` bills
  `cache_write_tokens` at the plain input rate, which underprices a real
  cache write (Anthropic charges a premium for those). Not fixed by
  widening `Pricing` speculatively — nothing in this repo requests prompt
  caching yet, so `cache_write_tokens` is always `0` in practice today.
  Flagged in code and `ROADMAP.md` for whoever turns on prompt caching.
- **History sent to the provider excludes `status="failed"` messages but
  does *not* strip reasoning blocks** — the opposite filtering from
  `messages.py`'s client-facing list endpoint, which strips reasoning by
  default and includes every status. §3.1 is explicit that reasoning
  blocks must be echoed back to providers that require it; the "omit by
  default" behavior in §5.3 is specifically about API responses to
  clients, not the backend's own provider calls. Easy rule to get
  backwards if working from `messages.py` as a template — called out
  explicitly in `chat.py`'s `_load_history()`.
- **No `status="incomplete"` in this slice.** §3.3 defines it as "the
  stream ended early (client disconnect, cancel, or truncation) but the
  partial content was persisted" — none of those three things can happen
  without a streaming connection to disconnect from or cancel. A
  provider-side failure here becomes `failed` (with whatever partial
  content arrived); `incomplete` is SSE-only and arrives with roadmap item
  4b.
- **`message_count`/`updated_at` bookkeeping finally implemented** —
  closes the gap flagged in both the messages-slice and conversations-slice
  `BUILD_LOG` entries ("nothing increments `message_count` yet"). Bumped
  once per *row created* (user message, assistant message), not per status
  transition — updating the assistant row from `pending` to `complete`/
  `failed` later doesn't bump it again.
- **Discovered, not fixed: `main.py` has no exception handler for
  FastAPI's own `RequestValidationError`**, only for `DomainError`. Every
  422 caused by FastAPI's own request validation (missing
  `Idempotency-Key`, an `extra="forbid"` violation, anywhere in this
  codebase, not just chat) returns FastAPI's default `{"detail": [...]}`
  shape, not the §2 envelope. Predates this slice, affects every endpoint
  equally — added to `ROADMAP.md`'s cross-cutting gaps rather than folding
  a global fix into an already-large slice. `test_chat_missing_idempotency_key_is_422`
  only asserts the status code for this reason, not the body shape.
- **A real test bug, not a code bug, cost real debugging time:**
  `test_chat.py`'s idempotency tests initially used fixed string literals
  (`"idem-1"`, `"idem-replay"`, `"idem-conflict"`) as the default/reused
  `Idempotency-Key`. Since this repo's tests share one persistent dev
  Postgres with no per-test isolation, and idempotency rows are looked up
  by `key` alone, a later test (or a *second run* of the same test file)
  silently replayed an *earlier* test's — or an earlier run's — cached
  response instead of exercising its own scenario, producing wrong-looking
  failures (`assert 0 == 2` for a message count, `assert 201 == 503` for
  an error case) that had nothing to do with `chat.py` itself. Fixed by
  generating a fresh `uuid.uuid4()` per test invocation instead of any
  fixed literal. This is the same class of problem
  `test_conversations.py`'s pagination test and `test_messages.py`'s
  cursor tests already learned to work around (assert exact IDs, not
  counts) — worth remembering for any future test that owns an identifier
  meant to be globally unique.

### Understand before the next step

- **The SSE half of the chat endpoint (roadmap item 4b) reuses almost
  everything here** — `_run_turn`'s accumulation logic, model/param
  resolution, and history loading all stay the same. What's new: framing
  events as they arrive instead of after the loop finishes, a `ping` timer
  wrapping the adapter's iterator from outside (ADR-0002 decision 6 — not
  `core/llm`'s job), `message_start`/`message_stop` (which need the
  message/run IDs `core/llm` deliberately doesn't know about), disconnect
  detection, and periodic (not just final) DB flushes.
- **`idempotency.check_or_claim()` returning `None` means "you must now do
  the real work and call `complete()` or `abandon()`"** — it's not a
  boolean "may I proceed" flag with commit semantics left to guesswork.
  Any future endpoint adding idempotency support should copy this exact
  three-function shape rather than inventing a variant.
- **`_get_adapter()` in `chat.py` is a single `if`, not a dispatch table.**
  The second provider adapter should turn this into a real
  `dict[str, ProviderAdapter]`-shaped lookup — deliberately not built for
  one entry.

### Deliberately deferred

- SSE (`Accept: text/event-stream`, §5.5) — roadmap item 4b, see above.
- `X-Params-Dropped` header — `chat.py` silently drops `reasoning_effort`/
  `response_format` rather than reporting them; no service→router channel
  exists yet for this.
- Capability enforcement before a provider call (§4) — the registry has
  the data, `chat.py` doesn't call it yet.
- `main.py`'s missing `RequestValidationError` handler (see Decisions) —
  cross-cutting, tracked in `ROADMAP.md`, not chat-specific.
- A provider→adapter dispatch table (see Understand-before-next-step).
- Cache-write pricing accuracy (see Decisions).
- Everything else already on the roadmap: `tools.py`, `files.py`,
  `titling.py` (blocked on a Groq adapter existing), cancellation, the four
  remaining provider adapters, rate-limit headers, real token verification,
  a real `users` table/FK, per-test DB isolation fixture.

---

## 2026-07-20 — Chat endpoint, SSE (roadmap item 4b)

**Built:** `Accept: text/event-stream` on the same `POST
/api/v1/conversations/{id}/messages` — the deferred half of the chat
endpoint. `ping` keepalive, `message_start`/`message_stop` framing,
per-block DB flushing, client-disconnect handling, and in-stream error
framing, all per §5.5. The explicit `POST /runs/{run_id}/cancel` endpoint
stayed deferred to roadmap item 7, per your call — it's a different
mechanism (server-initiated cancel, needs a persisted run-tracking table)
from the client-disconnect handling this slice does implement.

- `app/core/errors.py` — `DomainError.to_envelope(request_id)`, extracted
  from `main.py`'s exception handler so the SSE path's in-stream `error`
  frame and the normal HTTP error response build the identical §2 shape
  from one definition, not two.
- `app/main.py` — `domain_error_handler` now calls `exc.to_envelope()`
  instead of hand-assembling the same dict inline.
- `app/core/llm/adapter.py`, `anthropic_adapter.py` — `ProviderAdapter.stream()`'s
  return type widened from `AsyncIterator[LLMEvent]` to `AsyncGenerator[LLMEvent, None]`.
  Not cosmetic: the SSE path needs `.aclose()` on the adapter's generator to
  abort an in-flight provider call on disconnect, and `AsyncIterator` doesn't
  have that method — mypy caught this immediately when `emit_stream` tried
  to call it.
- `app/services/chat.py` — restructured around the real constraint this
  slice surfaced (see Decisions): `_validate_and_resolve()` (read-only) and
  `_persist_turn_start()` (mutating, only ever called after a confirmed
  non-replay idempotency claim) are now shared by both response shapes.
  `create_chat_message()` (JSON) is behavior-identical to before the
  refactor — reran the full existing test suite immediately after
  extracting these to confirm. New: `prepare_stream()` (`await`ed directly
  by the router) and `emit_stream()` (the actual SSE frame generator),
  plus `_replay_frames()` for idempotency replay.
- `app/api/v1/chat.py` — branches on `Accept`; the SSE branch `await`s
  `prepare_stream()` *before* constructing `StreamingResponse`, passes it
  `emit_stream()` as the body with §5.5's three required headers
  (`Cache-Control`, `X-Accel-Buffering`, `Connection`).
- `backend/tests/test_chat_stream.py` — 7 new tests: happy-path event
  sequence (text and tool-call), persistence matching the non-streaming
  path's outcome, mid-stream error framing, idempotency replay
  reconstruction (provider called once across two streamed requests, same
  as the non-streaming idempotency test), `ping` during simulated provider
  silence, and client-disconnect → `status="incomplete"`.
- `backend/tests/test_chat.py` — removed
  `test_chat_streaming_accept_header_is_rejected`. Its premise (SSE gets
  rejected) is exactly what this slice replaced on purpose; the real
  behavior is now covered by `test_chat_stream.py`.

**Verified for real:** `ruff check`/`ruff format --check` clean; `mypy app`
(strict, 30 files) clean; `check_layering.sh` clean; `pytest` green (61/61,
up from 54), the new SSE test file run three times in a row to check for
timing-related flakiness (none). Then ran the API for real and curled an
SSE request with no `ANTHROPIC_API_KEY` configured and one with an unknown
model — both returned clean pre-stream JSON errors (503, 400) rather than a
corrupted 200 stream, which is exactly the property the
`prepare_stream()`/`emit_stream()` split exists to guarantee. Queried
`idempotency_keys` directly afterward and confirmed both curled failures
left zero rows, same as the non-streaming slice's equivalent check.

### Decisions

- **Idempotency replay reconstructs a synthetic SSE sequence from the same
  final-response data the non-streaming path already stores, rather than
  recording the original stream's exact frame-by-frame chunking** — asked
  and confirmed. Zero changes to `idempotency.py`. §7's client obligations
  (buffer `input_json_delta` until `content_block_stop`, ignore unknown
  events) mean a client can't distinguish a reconstructed single-chunk
  replay from the original multi-chunk stream by the content it conveys.
- **`POST /runs/{run_id}/cancel` stays deferred to roadmap item 7** — asked
  and confirmed. `run_id` exists in every `message_start` frame (freshly
  generated per stream, including on replay) but is never persisted
  anywhere — nothing needs to look one up until that endpoint exists to
  need it. Client-disconnect handling, which *is* in this slice, is a
  different mechanism: the server noticing the client left, not the client
  asking the server to stop.
- **Found a real architectural constraint while writing the generator, not
  while planning it, and restructured around it:** a `StreamingResponse`'s
  body generator can't produce a clean pre-stream HTTP error. §5.5 is
  explicit that errors before the first byte use the normal error envelope
  with a real status code — but by the time an async generator's first
  exception would surface, `StreamingResponse` may have already committed
  headers (status 200) via ASGI. The fix: `prepare_stream()` (validation,
  idempotency claim, persistence) is a plain `await`ed coroutine the router
  calls *before* constructing `StreamingResponse` — a `DomainError` there
  propagates normally to `main.py`'s existing exception handler, exactly
  like the non-streaming path. Only `emit_stream()`, called after
  `prepare_stream()` already succeeded, is the actual generator, and it
  never raises — everything inside is caught and framed as an `error`
  event instead. Verified live via curl (see "Verified for real" above),
  not just asserted in a docstring.
- **Found and fixed a genuine `asyncio` correctness bug before it shipped,
  via empirical verification, not code review:** the natural-looking
  implementation of the ping timer —
  `asyncio.wait_for(agen.__anext__(), timeout=15)` retried in a loop — is
  broken. Wrote a 15-line throwaway script to check this *before* writing
  the real implementation, because async-generator-cancellation semantics
  are exactly the kind of thing that looks obviously correct and silently
  isn't: confirmed that `wait_for()` cancels its wrapped coroutine on
  timeout, and cancelling an async generator's in-flight `__anext__()`
  permanently exhausts it — every call after the first timeout raises
  `StopAsyncIteration` immediately, indistinguishable from the provider
  legitimately finishing, silently dropping the rest of the response. Fixed
  with `asyncio.wait({task}, timeout=15)` around a task created once and
  reused across repeated timeouts — `wait()` leaves a timed-out task alive
  in the background, so re-waiting on the *same* task lets it eventually
  complete for real. Verified this fix empirically too (a second throwaway
  script) before writing it into `chat.py`. Full writeup in ADR-0002's
  updated decision 6 — read it before touching this loop again.
- **DB flush granularity: at each `content_block_stop`, not on a wall-clock
  timer.** A block boundary is a natural checkpoint (a fully-formed piece
  of content, not a mid-word fragment) and needs no separate timer running
  concongruently with the ping/event loop — simpler than the alternative
  and still matches ARCHITECTURE.md's "accumulate + periodically flush...
  not per token."
- **`ProviderAdapter.stream()`'s return type is `AsyncGenerator`, not
  `AsyncIterator`** — a real type-precision fix, not a workaround. Every
  adapter is implemented as an async generator function and always has
  been; the interface just hadn't needed to say so until `emit_stream`
  needed `.aclose()`. `mypy --strict` caught the mismatch immediately.
- **`DomainError.to_envelope()` added to `core/errors.py`** so `main.py`'s
  HTTP handler and `chat.py`'s in-stream `error` frame build the identical
  §2 shape from one place. Small refactor, motivated directly by having a
  second real caller that needed the exact same dict — not speculative.

### Understand before the next step

- **`prepare_stream()` must always be `await`ed directly by a caller, never
  wrapped in a generator or otherwise deferred** — that's the entire
  mechanism that keeps pre-stream errors clean. Any future change to
  `app/api/v1/chat.py`'s SSE branch that moves this call inside
  `emit_stream()` (even accidentally, while refactoring) silently
  reintroduces the corrupted-response bug this slice exists to avoid.
- **The `asyncio.wait()`-around-a-persistent-task pattern in `emit_stream()`
  is the correct template for any future code that races an async
  generator against a timeout** — `wait_for()` looks like the obvious
  choice and is wrong for this specific case. Copy the pattern, don't
  rediscover the bug.
- **`_validate_and_resolve()` and `_persist_turn_start()` are now the
  shared foundation both response shapes build on.** A third response
  shape (there isn't one planned, but hypothetically) would reuse these
  exactly, not reimplement conversation/model/param resolution a third
  time.

### Deliberately deferred

- `POST /runs/{run_id}/cancel` and the run-tracking table it needs
  (roadmap item 7, see Decisions).
- Full-fidelity idempotency replay (recorded raw event log instead of
  reconstruction, see Decisions).
- Everything already deferred from the non-streaming chat session that
  this slice didn't touch: `X-Params-Dropped`, capability enforcement
  before a provider call, `main.py`'s missing `RequestValidationError`
  handler, a provider→adapter dispatch table, cache-write pricing accuracy,
  `tools.py`, `files.py`, `titling.py`, the four remaining provider
  adapters, rate-limit headers, real token verification, a real `users`
  table/FK, per-test DB isolation fixture.

---

## 2026-07-20 — Frontend wiring (Streamlit MVP)

**Built:** `frontend/streamlit_app/api_client.py` and `app.py` — both empty
stubs since the scaffolding session. A minimal chat UI against the SSE
endpoint the last two sessions built: sidebar conversation list with
create/delete, message history, and a streaming chat input.

- `api_client.py` — sync `httpx` (no asyncio; Streamlit's execution model
  reruns the whole script top-to-bottom per interaction, so there's no
  event loop to hang an async client off). `list_conversations`,
  `create_conversation`, `delete_conversation`, `list_messages`,
  `stream_chat_message` (a generator yielding parsed `{event, data}` SSE
  pairs, not just text — `app.py` needs `content_block_start`/`error`/
  `message_stop`, not only `text_delta`). Config via `AGENTOS_API_BASE_URL`/
  `AGENTOS_API_TOKEN` env vars — `os.getenv` directly, not a `config.py`
  module, since `CLAUDE.md`'s "config only via `app/config.py`" rule is
  `backend/app/`-scoped (confirmed by reading `check_layering.sh` itself:
  its `os.getenv` grep only covers `backend/app/`, not `frontend/`).
- `app.py` — sidebar (create/select/delete conversations, client-side
  `title: null` → "New conversation" placeholder per §5.2/§7 — never
  persisted or invented), message history via `list_messages`,
  `st.chat_input` → `st.write_stream` fed by a generator that pulls
  `text_delta` chunks out of `stream_chat_message`'s parsed events and
  surfaces `error` events via `st.error`. A 404 on `list_messages` (the
  selected conversation was deleted since last listed) resets selection
  instead of dead-ending on a permanent error screen.

**Verified for real, not just written — with a real gap in this
environment, disclosed rather than glossed over:** the Chrome extension
(`claude-in-chrome`) was not connected in this session, so the CLAUDE.md
instruction to "start the dev server and use the feature in a browser"
couldn't be followed literally. Built `docker compose up --build api
streamlit` for real (the streamlit service has no Dockerfile — installs
`streamlit`+`httpx` at container start, per the scaffolding session's
`docker-compose.yml`) and confirmed the container serves `200` at
`http://localhost:8501`. Then, in place of interactive browser clicks, used
Streamlit's own headless testing API
(`streamlit.testing.v1.AppTest.from_file("app.py")`) — this actually runs
`app.py`'s real script logic (imports, `st.*` calls, session state) without
a browser, not a mock. Against the live containers:
- Initial load: no exception, sidebar correctly listed every real
  conversation in the dev Postgres (leftover from earlier sessions' curl
  smoke tests), title placeholders rendered for the null-titled ones.
- Clicked "+ New conversation" (via `AppTest`'s button `.click().run()`),
  confirmed `session_state.conversation_id` was set to a real ID from the
  live API.
- Sent a message via `chat_input.set_value(...).run()`.

**Found a real UX bug this way, not through code review:** the first
attempt failed every send with `invalid_request: "No model specified and
the conversation has no default_model."` — `create_conversation()` wasn't
setting `default_model`, and `GET /api/v1/models` doesn't exist yet for the
UI to offer a picker. Fixed by hardcoding
`default_model="anthropic:claude-sonnet-4-5"` in `create_conversation()`
(see Decisions). Re-ran the same `AppTest` script after the fix: the error
changed to the *expected* one for this environment —
`provider_unavailable: "No API key configured for provider 'anthropic'."`
(503) — same failure the `core/llm` and chat-endpoint sessions' curl smoke
tests already hit, confirming the fix worked and the remaining error is an
environment limitation, not a bug. Checked the DB directly afterward
(`conversations.message_count`) and confirmed it was still `0` for that
conversation — correct, not a bug: the missing-API-key check happens in
`chat.py`'s `_validate_and_resolve()`, which runs *before* any persistence,
so a request that fails there should leave no trace, and doesn't.

Ran the full backend test suite (`pytest`, 61/61) after these changes to
confirm frontend-only work caused no backend regression, and
`./scripts/check_layering.sh` to confirm `frontend/` still imports nothing
from `backend/`.

### Decisions

- **`default_model` hardcoded in the client, not fetched from a registry
  endpoint.** `GET /api/v1/models` (§4) isn't built yet, and even once it
  is, `anthropic:claude-sonnet-4-5` is the only model with a real adapter —
  a picker UI would offer exactly one real choice today. Hardcoding is
  honest about current capability, not a workaround; revisit when a second
  adapter lands and picking a model becomes a real decision.
- **Chat sends go through SSE exclusively, not the non-streaming JSON
  variant.** Both exist server-side; the UI only needs one, and streaming
  is both the better demo of the last two sessions' work and the more
  natural fit for `st.chat_input`/`st.write_stream`.
- **`stream_chat_message()` yields parsed `{event, data}` pairs, not bare
  text.** A generator that only yielded `text_delta` strings would be
  simpler to feed directly to `st.write_stream`, but `app.py` also needs to
  react to `error` and `content_block_start` (for a tool-call caption) —
  collapsing to text-only in the client would throw that away before
  `app.py` ever saw it.
- **No pagination UI, no image/tool_result content-block rendering.**
  Matches `ROADMAP.md`'s own frontend scope ("conversation list, streaming
  render, title placeholder handling") — `files.py` and `tools.py` don't
  exist yet to ever produce those blocks in a real conversation this UI
  would render.
- **Verification used `AppTest`, a real Streamlit testing API, instead of
  skipping verification when the browser extension wasn't available.**
  Disclosed the gap rather than silently substituting a weaker check —
  `AppTest` runs the actual script (real imports, real `st.*` calls, real
  session-state transitions) against the actual live API, which is a
  materially stronger check than "the code looks right" even though it
  isn't the literal browser-click verification CLAUDE.md asks for.

### Understand before the next step

- **Every conversation created through this UI gets the same
  `default_model`.** There is no way today to create one pointed at a
  different provider/model — not a missing feature so much as nothing else
  existing to point it at yet.
- **`AppTest`'s `session_state` proxy doesn't support `.get()`** — use
  dict-style `at.session_state["key"]` instead, or it raises `AttributeError:
  get not found in session_state` (not a `KeyError`, which would at least
  look like a normal missing-key error). Hit this once already; save the
  next person the confusion.
- **The `docker compose` stack (postgres + api + streamlit) was left
  running** after this session, not torn down, since the actual deliverable
  is something to open in a browser at `http://localhost:8501` — unlike
  previous sessions' backend-only curl smoke tests, there's no equivalent
  "done, tear it down" moment here. Stop it by hand
  (`docker compose down`) when finished poking at it.

### Deliberately deferred

- A committed frontend test (`frontend/streamlit_app/test_app.py` using
  `AppTest`) — this session's verification script was one-off, not saved.
  Worth doing before this UI grows past trivial further changes.
- A model picker UI — blocked on `GET /api/v1/models` existing and there
  being more than one real adapter to choose between.
- Pagination in the conversation list and message history.
- Rendering for `image`, `tool_result` content blocks — blocked on
  `files.py`/`tools.py`.
- Everything else already on the roadmap: the four remaining provider
  adapters, `tools.py`, `files.py`, `titling.py`, cancellation, rate-limit
  headers, real token verification, a real `users` table/FK, per-test DB
  isolation fixture, `main.py`'s missing `RequestValidationError` handler.

---

## 2026-07-20 — Frontend wiring: real browser verification (follow-up)

**What happened:** the Chrome extension became available later the same
day. Re-verified the frontend slice above with actual browser clicks
instead of the `AppTest`-only fallback — and found two real bugs the
fallback couldn't have caught, both fixed this session.

### Bug 1: wrong API base URL inside the `streamlit` container

First real page load threw `httpx.ConnectError: [Errno 111] Connection
refused` right in the Streamlit UI. `api_client.py`'s
`AGENTOS_API_BASE_URL` default (`http://localhost:8000`) is correct for a
script run on the *host* (which is what the earlier `AppTest` verification
did — and why it never saw this), but inside the `streamlit` **container**,
`localhost` refers to that container itself, not the `api` container.
Exactly the same class of gotcha `docker-compose.yml` already documents a
fix for on the `api` service's own `DATABASE_URL`.

**Fixed:** added `AGENTOS_API_BASE_URL: http://api:8000` to the `streamlit`
service's `environment:` block in `docker-compose.yml`, mirroring the
`api` service's existing `DATABASE_URL` override and its WHY comment. Also
corrected that file's `streamlit:` comment, which still said "frontend/ has
no app code yet this session" — stale since this same day's earlier
session.

### Bug 2: the error message flashed and vanished

With bug 1 fixed, sending a message from a real browser produced no
visible outcome at all — no error, no message, just an empty chat pane.
The API's own access log confirmed the request *did* complete correctly
(`POST .../messages` → `503`, matching the missing-API-key case exactly as
designed) — so the bug was purely client-side rendering.

Root cause: `app.py` called `st.rerun()` **unconditionally** after every
send attempt. `st.error(...)` renders fine within the script run that calls
it, but the very next line's `st.rerun()` immediately discards that whole
render and starts a fresh script pass — so the error was real, correctly
computed, and shown for a fraction of a second, then wiped before a human
could read it.

**Why the earlier `AppTest` verification didn't catch this:** it did,
technically — `at.error` in that run showed the message. But that's because
`AppTest.run()` does not chase a triggered `st.rerun()` through an
additional pass the way a real live Streamlit session does; it captured
state mid-flight in a way a real browser never presents to a real user.
This is exactly the gap flagged (but not resolved) in the earlier entry's
Decisions section — real browser verification is a materially different,
stronger check than `AppTest`, not a redundant one.

**Fixed:** `app.py`'s send handler now tracks whether an error occurred (a
one-element list, not a bool + `nonlocal` — see below) and only calls
`st.rerun()` on success. On any error — pre-stream (raised as `ApiError`)
or mid-stream (an `error` SSE event, which doesn't raise) — the script
finishes its current run normally, leaving the error visible until the
user's next interaction.

**A second, smaller bug surfaced while fixing the first:** the initial fix
used `nonlocal had_error` inside the nested `_text_chunks()` generator.
`python -m py_compile` caught this before it ever reached a browser:
`SyntaxError: no binding for nonlocal 'had_error' found`. Cause: the whole
send-handling block is script-level code inside `if prompt:`, not inside a
`def` — `if`/`with` blocks don't create function scopes in Python, so there
was no enclosing *function* for `nonlocal` to bind to (this only affects
`nonlocal`/`global`; ordinary variable reads across such blocks work fine).
Fixed by mutating a one-element list (`had_error = [False]`,
`had_error[0] = True`) instead, which needs no scope-binding keyword at
all.

**Verified for real, this time via actual browser interaction:**
navigated to `http://localhost:8501`, confirmed the sidebar loaded the
real conversation list with no connection error, clicked "+ New
conversation", typed a message into `st.chat_input` and pressed Return,
and watched `Couldn't send message: No API key configured for provider
'anthropic'. (503)` render and **stay on screen** — the actual fix,
confirmed the way a real user would experience it. Checked
`read_console_messages` for JS errors (none). Reloaded the page fresh
(session state correctly resets — expected Streamlit behavior for a new
browser session, not a bug) and exercised the sidebar delete button — list
refreshed cleanly, no error. Reran the full backend test suite (61/61) and
`check_layering.sh` after these changes to confirm nothing outside
`frontend/`/`docker-compose.yml` was touched.

### Understand before the next step

- **`nonlocal` requires an enclosing `def`, not just an enclosing indented
  block.** `if`/`with`/`for` don't create Python scopes. A closure that
  needs to mutate a variable from a block-nested (not `def`-nested) outer
  scope needs a mutable container (list, dict, small object), not
  `nonlocal`.
- **`docker-compose.yml`'s `streamlit` service needs its own
  service-network env var overrides, same as `api`'s `DATABASE_URL`.** Any
  future env var `api_client.py` reads with a `localhost`-flavored default
  needs the same treatment there.
- **`AppTest` is a genuinely useful fallback, not a substitute, for real
  browser verification** — it caught the `default_model` bug fine but
  missed both bugs found this session (one is container-networking, entirely
  invisible to a host-run script by construction; the other is specifically
  about `st.rerun()` semantics `AppTest` doesn't reproduce faithfully).
  Prefer real browser clicks whenever the extension is available.

### Deliberately deferred

- Same list as the previous entry — nothing new deferred this follow-up,
  it only fixed bugs in what was already built.

---

## 2026-07-21 — Wiring the openai/groq/together adapters (roadmap item 6)

**Found, not built, first:** `openai_adapter.py`, `groq_adapter.py`, and
`together_adapter.py` already existed in the working tree, each a full
translation of its provider's Chat Completions wire format into the
normalized `LLMEvent` vocabulary, each with the same `__init__(api_key:
str)` shape as `anthropic_adapter.py`. `openai_adapter.py` and
`groq_adapter.py` each had a passing `respx`-mocked test file already.
None of the three were reachable from a real request, though:
`registry.yaml` still listed only the Anthropic model, and
`app/services/chat.py`'s `_get_adapter()` still had its original single
`if entry.provider != "anthropic": raise` — the exact "next-adapter work"
its own WHY comment named. This entry is about closing that gap, not about
writing the adapters themselves (they were someone else's — or an earlier
session's — work already).

**Built:**

1. **`registry.yaml`** — one model entry per newly-wired provider:
   `openai:gpt-4o`, `groq:llama-3.3-70b-versatile`,
   `together:meta-llama/Llama-3.3-70B-Instruct-Turbo`. Model choice for groq
   and the openai id match API_CONTRACT.md §4's own worked examples
   verbatim (that section already used `llama-3.3-70b-versatile` and
   `openai:gpt-4o` in passing) — together had no example to match, so that
   one model was picked by hand. Pricing and capabilities are **not**
   independently re-verified against each provider's live pricing page —
   same caveat the existing Anthropic entry already carried, now extended
   to all three (see the file's own updated header comment).

2. **`app/services/chat.py`'s `_get_adapter()`** — replaced the single `if`
   with a `_ADAPTER_CLASSES` dict mapping `provider -> factory`, one entry
   per adapter that exists. `gemini` still has no entry (no adapter to
   back it), so a request naming a gemini model still fails the same
   `provider.not_implemented` way it always did — nothing about that error
   path changed, only how many providers now avoid hitting it.
   - **WHY the dict is typed `dict[str, Callable[[str], ProviderAdapter]]`,
     not `dict[str, type[ProviderAdapter]]`:** tried the more obvious
     `type[...]` annotation first; mypy rejected `adapter_cls(api_key=...)`
     because `ProviderAdapter` (the `Protocol` in `adapter.py`) only
     declares `stream()` — it says nothing about how an implementation is
     constructed, on purpose (construction isn't part of the interface
     contract). All four concrete classes *happen* to share
     `__init__(api_key: str)`, but that's a fact about them, not something
     `adapter.py` promises. `Callable[[str], ProviderAdapter]` says
     "one-argument factory returning a ProviderAdapter" — true, and
     doesn't ask the Protocol to lie about its own shape.
   - The api-key lookup (`getattr(settings, f"{entry.provider}_api_key")`)
     was already generic before this session — `registry.py`'s
     `is_available()` used the same `{provider}_api_key` naming
     convention already. Reused it rather than adding a second lookup.

3. **`tests/test_together_adapter.py`** — didn't exist; openai and groq
   each had one already, together didn't. Mirrors `test_groq_adapter.py`'s
   six cases (text, tool call, `length` → `max_tokens`, pre-stream rate
   limit, unknown-model, insufficient-quota) but with fixtures reflecting
   `together_adapter.py`'s own documented quirk: Together puts `usage` on
   the *same* chunk that carries `finish_reason`, not a trailing
   empty-`choices` chunk the way OpenAI/Groq do — confirmed by reading the
   adapter's module docstring, not by hitting the real API. **Said so
   explicitly in the test file's own docstring**, in contrast to
   `test_groq_adapter.py`/`test_openai_adapter.py`, which do claim real
   live verification — an honest test file shouldn't borrow a stronger
   verification claim than what actually happened for it.

4. **`app/core/llm/README.md`** — the "don't exist yet" list was stale
   (named `openai.py`/`together.py`/`groq.py`, which was never even this
   repo's actual naming convention). Updated to name the real
   `<provider>_adapter.py` files and added a step to "How to add a new
   one" for the `_ADAPTER_CLASSES` entry `chat.py` now needs.

**Verified:** `make lint` (ruff + mypy) clean. Started Colima (was not
running) and `docker compose up -d postgres` to run the **real** test
suite rather than trust the DB-dependent tests would pass untested — 79/80
passed; see "Understand before the next step" for the one failure and why
it's pre-existing and unrelated. All four adapters' unit tests (35 total,
DB-independent) pass in isolation too. Manually exercised
`app.services.chat._get_adapter()` against every `registry` entry with all
four provider keys set, confirming each resolves to its own adapter class
(not just that the code type-checks).

### Understand before the next step

- **A `Protocol`'s shape is exactly what it declares, nothing an
  implementation happens to also do.** `ProviderAdapter` declaring only
  `stream()` means mypy will not let you call an implementation's
  constructor through a `type[ProviderAdapter]`-typed value, even though
  every real implementation today takes the same `__init__(api_key: str)`.
  If that ever needs to be part of the contract (e.g., a future adapter
  needs a second constructor arg), add it to the Protocol deliberately —
  don't work around the type error by widening `Callable[[str], ...]`
  into `Callable[..., ...]`, which would silently stop catching a
  mismatched adapter constructor.
- **`test_chat_bumps_message_count_and_updated_at` mixes two clocks and
  will fail under clock drift.** The conversation row's `updated_at` is
  set once by Postgres's own `func.now()` (row creation, via the
  `server_default` in `app/models/conversation.py`) and later by
  `chat.py`'s `_bump_conversation()` using Python's `datetime.now(UTC)` —
  two different machines' clocks (the Postgres container vs. the process
  running pytest) being compared with a strict `>`. This session hit it
  once, immediately after `colima start` reported adjusting the guest
  clock by `-323ms` on boot — a transient VM-clock-skew artifact of
  restarting Colima, not a code change in this session. Flagged in
  ROADMAP.md rather than fixed here since the real fix (stop comparing two
  clocks — read the DB's own value back, or timestamp everything from one
  place) touches `chat.py`'s bump logic, outside this session's scope.
- **Starting Colima/Docker was necessary to trust `make test` at all.**
  Before that, every DB-touching test failed with a connection refused —
  not because of anything in this session's changes, but because nothing
  had brought Postgres up yet. Unit-tier tests (adapters, pricing,
  registry) don't need it and are a fast first signal, but they can't
  stand in for the real integration suite CLAUDE.md's `make test` policy
  expects to pass before a commit.

### Deliberately deferred

- **`gemini` adapter** — still not written. Same "additive once one
  adapter proves the abstraction" reasoning ROADMAP.md already stated;
  nothing about this session changes that reasoning, it just shrinks the
  remaining list from four providers to one.
- **Capability enforcement before a provider call** and **`X-Params-Dropped`
  reporting** — both already-flagged gaps in ROADMAP.md, untouched this
  session. Wiring three more adapters makes both gaps slightly more
  visible (three more providers whose capability mismatches or dropped
  params go unreported) but neither was in scope for "make the adapters
  reachable."
- **The `updated_at` clock-mixing test fragility** — see above; flagged,
  not fixed.
