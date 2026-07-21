"""Auth router — register/login/logout (API_CONTRACT.md §1).

Role: HTTP surface for account creation and session issuance. Unlike every
other router in this package, this one does NOT follow "validate -> call
service -> return a schema" — it composes fastapi-users' own pre-built
routers, which own that logic internally. See app/core/auth/README.md and
docs/DECISIONS/0003 Auth Layering.md for why.
Called by: app/main.py (included under /api/v1). Calls app.core.auth.users,
app.core.auth.backend, app.schemas.user.
Gotcha: `/login` takes `application/x-www-form-urlencoded`
(OAuth2PasswordRequestForm — field named `username`, even though it holds
the email), not JSON like every other endpoint in this API. This is a
well-known convention from the OAuth2 password grant, not a contract
inconsistency — documented in docs/API_CONTRACT.md §1.
See: docs/API_CONTRACT.md#1-authentication
"""

from fastapi import APIRouter

from app.core.auth.backend import auth_backend
from app.core.auth.users import fastapi_users
from app.schemas.user import UserCreate, UserRead

router = APIRouter(prefix="/auth", tags=["auth"])

# Gives POST /auth/login and POST /auth/logout.
router.include_router(fastapi_users.get_auth_router(auth_backend))
# Gives POST /auth/register.
#
# WHY get_users_router() (GET/PATCH /users/me, admin user CRUD) is not
# mounted: not asked for by this feature — trivial to add later since
# `fastapi_users` already exists here, but out of scope now.
router.include_router(fastapi_users.get_register_router(UserRead, UserCreate))
