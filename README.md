# AgentOS

A chat backend — FastAPI + Postgres, with a disposable Streamlit MVP frontend — built to
grow into an agent runtime. Every request goes through a normalized, provider-agnostic
LLM layer (Anthropic, OpenAI, Groq, Together today; Gemini pending an adapter), so
nothing above `core/llm/` ever knows which provider actually answered a message.

Full status: [`docs/ROADMAP.md`](docs/ROADMAP.md). Wire format: [`docs/API_CONTRACT.md`](docs/API_CONTRACT.md).
Layering rules: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Narrative build history:
[`docs/BUILD_LOG.md`](docs/BUILD_LOG.md).

## What's here today

- **Real accounts** — email/password signup, JWT auth, per-user token usage limits.
- **Conversations & messages** — full CRUD, cursor pagination, soft delete.
- **Streamed chat** — SSE responses with `ping` keepalive, client-disconnect handling,
  and idempotent retries via an `Idempotency-Key` header.
- **Four live LLM providers** — Anthropic, OpenAI, Groq, Together, behind one
  normalized adapter interface. Model discovery is a hybrid: a small curated catalog
  (capabilities/pricing) enriches whatever each provider's own API reports live.
- **A Streamlit MVP UI** — login/signup, a chat view, and a Settings page for picking
  which provider and model to use next.

Not built yet: tool calling, file uploads, conversation titling, and a Gemini adapter —
see the roadmap for the full list.

## Architecture, in one picture

```
   HTTP client  (Streamlit MVP today → Next.js later)
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
                       Anthropic · OpenAI · Groq · Together · (Gemini)
```

Dependencies point downward only. `app/services/` never imports `fastapi`, so the same
business logic is callable from a future agent loop or CLI, not just an HTTP request.
Details: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Tech stack

- **Backend**: Python 3.12, FastAPI, SQLAlchemy (async) + Postgres, Alembic, fastapi-users
  (JWT auth), httpx (every provider call — no vendor SDKs), pydantic-settings.
- **Frontend**: Streamlit (deliberately disposable — see `Claude.md`).
- **Tooling**: `uv` for Python dependency management, `ruff` + `mypy` for lint/type
  checking, `pytest` for tests, Docker Compose for local dev.

## Prerequisites

- Docker and Docker Compose
- [`uv`](https://docs.astral.sh/uv/) — for running tests, lint, and migrations on the host
- At least one LLM provider API key (Anthropic, OpenAI, Groq, or Together) to actually
  chat — the app runs without any configured, but every model shows `available: false`

## Installation

1. **Clone and configure environment variables**

   ```bash
   git clone https://github.com/Hon3y9718/AgentOS.git
   cd AgentOS
   cp .env.example .env
   ```

   Edit `.env` and set at minimum:

   | Variable | Required | Notes |
   |---|---|---|
   | `DATABASE_URL` | yes | e.g. `postgresql+asyncpg://agentos:agentos@localhost:5432/agentos` for host-run tools (pytest, alembic) — Docker Compose overrides this internally for the `api` container |
   | `SECRET_KEY` | yes | signs JWTs; any long random string for local dev |
   | `ANTHROPIC_API_KEY` | no | enables the Anthropic provider |
   | `OPENAI_API_KEY` | no | enables the OpenAI provider |
   | `GROQ_API_KEY` | no | enables the Groq provider |
   | `TOGETHER_API_KEY` | no | enables the Together provider |
   | `GEMINI_API_KEY` | no | reserved — no adapter exists yet |
   | `LOG_LEVEL` | no | defaults to `INFO` |
   | `ENABLE_LIVE_MODEL_REFRESH` | no | defaults to `true`; set `false` to skip live per-provider model discovery and use only the curated catalog |

   Then symlink it into `backend/`:

   ```bash
   ln -s ../.env backend/.env
   ```

   Host-run tools (`alembic`, `pytest` outside its own fixture defaults) load config via
   `pydantic-settings`, which resolves `.env` relative to the process's working
   directory — `uv run --directory backend ...` runs with `backend/` as that directory,
   so without this symlink those commands can't see the root `.env` at all. Docker
   Compose doesn't need this — it injects `.env` into the `api` container directly.
   The symlink is already covered by `.gitignore`'s `.env` rule at every directory level.

2. **Start the stack**

   ```bash
   make dev
   ```

   This runs `docker compose up`: Postgres on `5432`, the API on `8000`, and the
   Streamlit UI on `8501`.

3. **Apply database migrations** (not automatic yet — see `docs/ROADMAP.md`)

   ```bash
   uv run --directory backend alembic upgrade head
   ```

   If this fails with `database_url: Field required`, your `.env` predates the
   `DATABASE_URL` convention — add the line from the table in step 1 by hand.

4. **Open the app**

   - Streamlit UI: [http://localhost:8501](http://localhost:8501) — sign up for an
     account on first visit.
   - API docs (Swagger UI): [http://localhost:8000/docs](http://localhost:8000/docs)
   - Health check: [http://localhost:8000/health](http://localhost:8000/health)

## Running tests and lint

```bash
make lint    # ruff check --fix && ruff format && mypy app — no dependencies needed

# make test needs Postgres reachable (via `make dev`, or `docker compose up -d postgres`)
make test    # pytest — must pass before any commit
```

## Creating a new migration

```bash
make migrate m="describe the change"
```

## Project layout

```
backend/app/api/v1/     routers — thin, no business logic
backend/app/services/   orchestration, transactions (no fastapi import — CI-enforced)
backend/app/core/llm/   provider adapters, the model registry, cost math
backend/app/core/auth/  fastapi-users composition (JWT, password hashing)
backend/app/models/     SQLAlchemy tables
backend/app/schemas/    the wire format, in and out
frontend/streamlit_app/ the disposable MVP UI
docs/                   the contract, architecture rules, decisions, build history
```

Each package has its own `README.md` with more detail — read the one nearest the code
you're touching before grepping.

## Contributing

This is a learning project — see [`Claude.md`](Claude.md) for the conventions AI
assistants (and human contributors) follow here: layering rules, documentation policy,
and what needs a plan before landing.
