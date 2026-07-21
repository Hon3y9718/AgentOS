"""fastapi-users dependency chain (API_CONTRACT.md §1).

Role: the DB-adapter -> UserManager -> FastAPIUsers dependency chain, and the
one dependency app/api/v1/deps.py's CurrentUser wraps.
Called by: app/api/v1/auth.py (register/login routers), app/api/v1/deps.py
(current_active_user). Calls app.core.auth.manager, app.core.auth.backend,
app.db.session, app.models.user.
Gotcha: `current_active_user` raises a bare fastapi.HTTPException(401) on a
missing/invalid/expired token — not our DomainError — main.py's
http_exception_handler is what translates it into the §2 envelope.
See: docs/DECISIONS/0003 Auth Layering.md
"""

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends
from fastapi_users import FastAPIUsers
from fastapi_users.db import SQLAlchemyUserDatabase

from app.core.auth.backend import auth_backend
from app.core.auth.manager import UserManager
from app.db.session import DbSession
from app.models.user import User


async def get_user_db(session: DbSession) -> AsyncGenerator[SQLAlchemyUserDatabase[User, str]]:
    yield SQLAlchemyUserDatabase(session, User)


# WHY an Annotated alias, not `Depends(get_user_db)` inline below: ruff/bugbear's
# B008 flags function calls in argument defaults — same reasoning as
# DbSession (app/db/session.py) and CurrentUser (app/api/v1/deps.py).
_UserDb = Annotated[SQLAlchemyUserDatabase[User, str], Depends(get_user_db)]


async def get_user_manager(user_db: _UserDb) -> AsyncGenerator[UserManager]:
    yield UserManager(user_db)


fastapi_users = FastAPIUsers[User, str](get_user_manager, [auth_backend])

# WHY active=True, not also verified=True: this MVP has no email-verification
# flow wired up (no get_verify_router mounted) — requiring verified=True
# would make every freshly registered account permanently unable to
# authenticate. Revisit together if verification ships for real.
current_active_user = fastapi_users.current_user(active=True)
