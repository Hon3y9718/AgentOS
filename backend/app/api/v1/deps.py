"""Auth dependency (API_CONTRACT.md §1).

Role: resolves the request's user from the Authorization header. Every route
already goes through this — real token verification (fastapi-users' JWT
strategy, app.core.auth) swapped in behind this one function, exactly as the
MVP stub's original docstring anticipated.
Called by: app/api/v1/conversations.py, messages.py, chat.py (every
authenticated router).
Calls app.core.auth.users.
Gotcha: a missing/invalid/expired Bearer token raises a bare
fastapi.HTTPException(401), not our DomainError — main.py's
http_exception_handler is what turns it into the §2 envelope, not this file.
See: docs/API_CONTRACT.md#1-authentication
"""

from typing import Annotated

from fastapi import Depends

from app.core.auth.users import current_active_user
from app.models.user import User


async def get_current_user(user: Annotated[User, Depends(current_active_user)]) -> str:
    """Resolve the caller's user ID from a verified Bearer JWT.

    Raises:
        fastapi.HTTPException: 401, via app.core.auth.users.current_active_user
            — missing, malformed, expired, or otherwise unresolvable token.
    """
    return user.id


# WHY an Annotated alias, not `Depends(get_current_user)` inline at each call
# site: ruff/bugbear's B008 flags function calls in argument defaults, and
# this is the FastAPI-recommended way to avoid it — the Depends() call moves
# into type metadata instead of a mutable default.
CurrentUser = Annotated[str, Depends(get_current_user)]
