# AgentOS

Chat backend (FastAPI) + Streamlit MVP UI, becoming an agent runtime.
Streamlit is disposable — Next.js replaces it. Frontend NEVER imports backend code.

## Commands
- `make dev` – docker compose up (postgres + api + streamlit)
- `make test` – pytest; must pass before any commit
- `make lint` – ruff check --fix && ruff format && mypy app
- `make migrate m="msg"` – alembic autogenerate + upgrade

## Read before working
- `docs/API_CONTRACT.md` – the wire format. Authoritative over the code.
- `docs/ARCHITECTURE.md` – layering rules.
- `docs/DOCS_POLICY.md` – how to document (do it inline, in the same pass).
- `docs/DECISIONS/` – ADRs. 0002 covers providers.
- The package README nearest the code you're touching. Read it before grepping.

## Architecture rules
- Logic in `app/services/`. **No `fastapi` import in that package** — CI enforces it.
  Raise domain errors from `app/core/errors.py`; the API layer maps them to HTTP.
- Routers are thin: validate → call service → return a Pydantic schema. Never a dict,
  never a top-level array.
- All LLM calls go through `app/core/llm/`. Never import a provider SDK elsewhere.
- Providers: openai, anthropic, together, groq, gemini. Model IDs are `provider:model`.
  Capabilities come from `core/llm/registry.yaml`, never from a runtime probe.
- Message content is always a list of typed blocks, never a string.
- Streaming is SSE with named events, not WebSockets. See API_CONTRACT §5.5.
- Config only via `app/config.py` (pydantic-settings). No `os.getenv` elsewhere.

## Documentation (see DOCS_POLICY)
Written in the same pass as the code, never as a follow-up turn.
- Every file: a ≤6-line module header — what · role · callers/callees · gotcha · link.
- Public functions: docstring with args, returns, raises, one line of why.
- Non-obvious lines: `WHY:` or `GOTCHA:` comment. Nothing else gets a comment.
- Package README updated when a file is added.
- Never restate a fact that lives in API_CONTRACT, ARCHITECTURE, or an ADR — link.

## Conventions
- Python 3.12, async throughout, full type hints.
- Request models use `extra="forbid"`.
- Every external call has an explicit timeout.
- Money is a decimal string. Never a float.
- Never log keys, tokens, prompts, tool args, or message content.

## Do not
- Edit `alembic/versions/*` by hand.
- Add a dependency without asking.
- Write code before showing me a plan for anything touching >3 files.
- Widen the contract to make a test pass.