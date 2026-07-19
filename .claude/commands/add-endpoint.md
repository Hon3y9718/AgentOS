---
description: Add or modify a v1 API endpoint, contract-first, with tests
argument-hint: <method> <path> — <what it does>
allowed-tools: Read, Grep, Glob, Edit, Write, Bash(make *), Bash(pytest *), Bash(ruff *), Bash(alembic *), Bash(git diff *), Bash(git status)
---

Implement this endpoint: **$ARGUMENTS**

## Read first

Read these before writing anything. Do not skim.

- `docs/API_CONTRACT.md` — §2 error envelope, §3 domain model, §5 endpoints, §6 limits
- `docs/ARCHITECTURE.md` — layering rules
- `CLAUDE.md`
- The nearest existing sibling endpoint under `backend/app/api/v1/` and its tests

If the endpoint already appears in `API_CONTRACT.md`, that spec wins over anything in my
prompt. Tell me about the discrepancy instead of silently choosing.

## Phase 1 — Plan. Stop and wait for approval.

Do not write or edit any file in this phase. Produce:

1. **Contract diff.** The exact section to add to or change in `docs/API_CONTRACT.md`:
   method, path, auth, request schema, response schema, status codes, error types this
   endpoint can emit, headers, limits. Write it as final prose, not a summary.
2. **File list.** Every file to create or modify, with a one-line reason each.
3. **Data model impact.** New tables, columns, or indexes. Whether a migration is needed.
   Whether it is backwards-compatible with rows already in the DB.
4. **Provider impact.** If this touches the LLM path: which of the five providers
   (openai, anthropic, together, groq, gemini) behave differently here, and how the
   adapter layer normalizes that. If a provider cannot support it, say so — do not
   silently degrade.
5. **Test list.** Named test cases, unit and integration, including the failure cases.
6. **Open questions.** Anything genuinely ambiguous. Ask rather than assume; a wrong
   assumption baked into a contract is expensive to remove.

Then stop. Wait for me to approve or amend.

## Phase 2 — Implement, in this order

Contract before code, schema before logic, tests before implementation.

1. **`docs/API_CONTRACT.md`** — apply the approved diff. Add a changelog row in §8.
2. **`backend/app/schemas/`** — request and response Pydantic models.
   - `model_config = ConfigDict(extra="forbid")` on every request model.
   - Every field gets an explicit type. No bare `dict` or `Any` in a public schema.
   - Field-level constraints mirror §6 limits — do not enforce limits in the router.
3. **Migration**, if needed. `make migrate m="..."`. Read the generated file and confirm
   it matches intent. Never hand-edit an existing applied migration; add a new one.
4. **`backend/app/models/`** — SQLAlchemy models, if the migration added anything.
5. **`backend/app/services/`** — the actual logic.
   - Zero `fastapi` imports in this package. If you reach for `HTTPException`, stop:
     raise a domain error from `app/core/errors.py` instead.
   - Takes and returns domain objects or schemas, never `Request` or `Response`.
   - Every external call gets an explicit timeout.
   - All DB work happens in one transaction per request unless there is a stated reason.
6. **`backend/app/api/v1/`** — the router. Thin: validate, call the service, return a
   schema. If a router function exceeds roughly 20 lines, logic has leaked into it.
   - Declare `response_model` and `status_code` explicitly.
   - Declare `responses={...}` for every non-2xx status this endpoint can return, so the
     generated OpenAPI is honest.
   - Auth via the `get_current_user` dependency. Never read identity from the body.
7. **Wire the router** in `backend/app/api/v1/__init__.py` if it is new.
8. **Tests** in `backend/tests/`.
   - Unit tests for the service with the DB and providers faked.
   - Integration tests through the real ASGI app against the test database.
   - Required cases: happy path; `422` on a malformed body; `422` on an unknown extra
     field; `401` unauthenticated; `404` for another user's resource; the relevant §6
     limit being exceeded.
   - If streaming: assert the full event sequence and ordering, a mid-stream error, and
     a client disconnect persisting partial content as `incomplete`.
   - If it mutates state: assert idempotency-key replay returns the original result, and
     that replay with a changed body returns `409`.
9. **`frontend/streamlit_app/api_client.py`** — add the method. This is the only frontend
   file permitted to know about HTTP. Do not touch Streamlit UI code unless I ask.

## Phase 3 — Verify

Run, and fix until green:

```
make lint
make test
```

Then confirm each of these out loud, one line each. Do not claim a check passed without
having actually run or read something that proves it.

- [ ] `API_CONTRACT.md` updated and matches the implementation exactly
- [ ] Generated OpenAPI at `/api/v1/openapi.json` matches the contract
- [ ] No `fastapi` import anywhere under `app/services/`
- [ ] Every error path returns the §2 envelope with a mapped `type` — no raw provider codes
- [ ] Every log line in the new path carries the request ID
- [ ] No secret, token, prompt body, or user content in any log line
- [ ] New env vars added to `.env.example` and `app/config.py`
- [ ] Migration applies cleanly and downgrades cleanly on a fresh database
- [ ] Nothing in `frontend/` imports from `backend/`

## Never

- Add a dependency without asking.
- Write to `alembic/versions/*` by hand.
- Add a `try/except` that swallows an exception without logging and re-raising a typed error.
- Return a bare `dict` or a top-level JSON array from any endpoint.
- Use `time.sleep`, blocking file IO, or a synchronous HTTP client in an async path.
- Widen the contract to make a test pass.

Finish by showing me `git diff --stat` and a two-sentence summary of what changed.