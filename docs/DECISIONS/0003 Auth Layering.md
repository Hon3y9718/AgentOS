# ADR-0003: Auth Layering

**Status:** accepted, 2026-07-21.

**Context:** `ARCHITECTURE.md` and `CLAUDE.md` require `app/services/` to
never import `fastapi` — logic there must be callable without an HTTP
request existing, enforced by `scripts/check_layering.sh` in CI. Adding real
email/password authentication meant choosing `fastapi-users` (over a
hand-rolled `pyjwt`+`argon2-cffi` implementation), which doesn't fit that
shape: its `BaseUserManager` hooks take an optional `Request`, and its
router factories (`get_register_router`, `get_auth_router`) build
`APIRouter`s internally rather than being thin routers that call into a
service. This ADR settles where the resulting code actually lives, and how
its errors reach the client in the shape `API_CONTRACT.md` §2 requires.

---

## Decision 1: fastapi-users wiring lives in `app/core/auth/`, not `app/services/`

`app/core/auth/` holds `UserManager`, the JWT `AuthenticationBackend`, the
`FastAPIUsers` instance, and the dependency chain behind
`current_active_user`. `app/services/users.py` — genuine domain logic, the
per-user token usage check/increment — has zero `fastapi` import and lives
where every other service does.

**Alternatives considered:** (a) put all of it in `app/services/auth.py`
anyway, accepting the layering violation as a one-off exception; (b) wire
fastapi-users directly inside `app/api/v1/auth.py`, with no intermediate
package.

**Why `core/auth/` won:** `scripts/check_layering.sh` only greps
`app/services/` for `fastapi`/`from fastapi` imports — `app/core/` is
already home to exactly this kind of framework-coupled infrastructure
(`app/core/telemetry/middleware.py` imports Starlette's `Request`/`Response`
directly). Treating fastapi-users wiring as infrastructure the API layer
composes — the same relationship `api/v1/chat.py` has with `core/llm/` — is
more honest than forcing it into `services/` and pretending the layering
rule doesn't apply, and keeps `app/api/v1/auth.py` itself thin (it only
composes routers, it doesn't construct `UserManager`/`SQLAlchemyUserDatabase`
inline).

**Consequence:** `app/core/auth/` is the one package under `core/` whose
contents are not callable without a FastAPI request/dependency-injection
context — unlike `core/llm/`, which `ARCHITECTURE.md` requires to work
standalone. A future reader should not assume every `core/` package shares
that property.

---

## Decision 2: `app/api/v1/auth.py` composes fastapi-users' routers, not thin handlers calling a service

Every other router in `app/api/v1/` follows "validate → call service →
return a schema" (`app/api/v1/README.md`). `auth.py` instead does
`router.include_router(fastapi_users.get_auth_router(auth_backend))` and the
same for `get_register_router` — the request handling, password
verification, and token issuance all happen inside fastapi-users' own code.

**Why not hand-write `/register` and `/login` as thin handlers calling
`UserManager` directly:** that's strictly more code for identical behavior
— fastapi-users' routers already do exactly the validate/call/return
sequence internally, just against its own `UserManager` instead of an
`app/services/` module. Reimplementing them would mean maintaining a
parallel copy of logic (rate-limiting login attempts, safe user creation,
password verification timing) that the library already gets right.

**Consequence:** `app/api/v1/auth.py`'s own module docstring flags this
explicitly, so a future reader doesn't take it as the template for a new
router.

---

## Decision 3: fastapi-users' `HTTPException`s are bridged into the §2 envelope in `app/main.py`

fastapi-users' routers and its `Authenticator` dependency (behind
`current_active_user`, i.e. every `CurrentUser` use via
`app/api/v1/deps.py`) raise plain `fastapi.HTTPException` with
library-specific `detail` shapes (`ErrorCode.LOGIN_BAD_CREDENTIALS`,
`ErrorCode.REGISTER_USER_ALREADY_EXISTS`, a dict for
`REGISTER_INVALID_PASSWORD`) and status codes that don't match the
contract — login failure is `400` by default, not `401`; a missing/invalid
token is `401` with `detail: null`, not a `DomainError`.

**Why one more handler next to `domain_error_handler`, not a try/except
inside `auth.py`:** `app/main.py` is already "the only place a `DomainError`
becomes an HTTP response" — extending that same responsibility to
fastapi-users' exceptions keeps the translation in one place instead of
scattering `except HTTPException` blocks wherever fastapi-users code might
raise one (which includes every protected router, not just `auth.py`,
because `current_active_user` is used everywhere).

**Why the handler falls through to FastAPI's default behavior for anything
unrecognized**, rather than mapping every non-matched case to
`internal_error`: `@app.exception_handler(HTTPException)` intercepts *every*
`HTTPException` raised anywhere in the app, including ones with nothing to
do with auth — Starlette's own `404` for an unmatched route, `405` for a
wrong method. Forcing those through a fastapi-users-shaped mapping would
turn a correct `404` into an incorrect `500`. The handler calls
`fastapi.exception_handlers.http_exception_handler` (FastAPI's own default)
for anything it doesn't recognize, so behavior for non-auth `HTTPException`s
is provably unchanged.

**Consequence:** adding a new fastapi-users-raised `ErrorCode` (e.g. if
`get_verify_router`/`get_reset_password_router` are mounted later) requires
a matching branch in `app/main.py`'s `_map_fastapi_users_exception`, or that
error reaches the client as FastAPI's raw `{"detail": ...}` shape instead of
the §2 envelope — not a silent failure, but a contract violation worth
catching in review.

---

## What this ADR deliberately leaves open

Email verification and password reset (`get_verify_router`,
`get_reset_password_router`) are not mounted — `UserManager` has the
required secrets wired (reusing `settings.secret_key`, see
`app/config.py`'s docstring) but no endpoint exposes either flow yet.
`current_active_user` is checked with `active=True` only, not `verified=True`,
specifically so this doesn't lock out every registered account. Token
revocation (a blocklist for `/auth/logout`) is also out of scope — the JWT
strategy is fully stateless, so logout is a client-side no-op against this
backend.
