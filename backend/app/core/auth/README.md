# core/auth/

fastapi-users wiring: `UserManager` (password policy, ID parsing, post-register
hook), the JWT authentication backend, and the `FastAPIUsers` instance that
`app/api/v1/auth.py` and `app/api/v1/deps.py` both depend on.

## Why this lives under `core/`, not `services/`

`ARCHITECTURE.md` and `CLAUDE.md` require `app/services/` to never import
`fastapi` — logic there must be callable without an HTTP request existing.
`fastapi-users`' `BaseUserManager` doesn't fit that: its hooks take an
optional `Request`, and its router factories (`get_register_router`,
`get_auth_router`) build `APIRouter`s internally rather than being thin
routers that call into a service. This package is deliberately the same kind
of exception `app/core/telemetry/middleware.py` already is — framework-coupled
infrastructure that the API layer composes, not domain logic. `scripts/check_layering.sh`
only forbids `fastapi`/`starlette` imports under `app/services/`, never under
`app/core/`, so this doesn't trip CI — but the exception is deliberate, not an
oversight. See `docs/DECISIONS/0003 Auth Layering.md` for the full reasoning.

The one piece of genuine domain logic this feature needed — checking and
incrementing a user's token usage — is **not** here. It lives in
`app/services/users.py`, has zero `fastapi` import, and is called from
`app/services/chat.py` like any other service function.

## What lives here

- `manager.py` — `UserManager(BaseUserManager[User, str])`: password length
  policy (`validate_password`), string-id passthrough (`parse_id`), and a
  PII-safe post-registration log hook.
- `backend.py` — the JWT `AuthenticationBackend` (`BearerTransport` +
  `JWTStrategy`), signed with `app.config.settings.secret_key`.
- `users.py` — `get_user_db`/`get_user_manager` dependencies, the
  `FastAPIUsers[User, str]` instance, and `current_active_user` — the
  dependency `app/api/v1/deps.py`'s `get_current_user` wraps.

## What must never live here

- Anything with no reason to touch `fastapi`/`fastapi_users` types directly —
  that belongs in `app/services/`.
- Business rules unrelated to authentication mechanics (e.g. the usage-limit
  check) — see `app/services/users.py`.
