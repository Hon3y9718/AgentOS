"""Auth dependency — MVP stub (API_CONTRACT.md §1).

Role: resolves the request's user from the Authorization header. Real
dependency, fake resolution — every route already goes through this so
swapping in real token verification later touches one file.
Called by: app/api/v1/conversations.py (and every future authenticated router).
Calls app.core.errors.
Gotcha: the token's value is never checked in MVP — only that a Bearer header
is present — so this must not be mistaken for real authentication.
See: docs/API_CONTRACT.md#1-authentication
"""

from typing import Annotated

from fastapi import Depends, Header

from app.core.errors import UnauthenticatedError

# WHY a fixed constant, not a row in a (nonexistent) users table: §1's MVP
# auth resolves every token to one development user. Real multi-user auth
# swaps this function's body, not its signature or callers.
DEV_USER_ID = "user_dev"


async def get_current_user(authorization: str | None = Header(default=None)) -> str:
    """Resolve the caller's user ID from the Authorization header.

    Raises:
        UnauthenticatedError: header missing or not a Bearer token.
    """
    if authorization is None or not authorization.startswith("Bearer "):
        raise UnauthenticatedError(
            "Missing or malformed Authorization header.",
            code="auth.missing_bearer_token",
        )
    return DEV_USER_ID


# WHY an Annotated alias, not `Depends(get_current_user)` inline at each call
# site: ruff/bugbear's B008 flags function calls in argument defaults, and
# this is the FastAPI-recommended way to avoid it — the Depends() call moves
# into type metadata instead of a mutable default.
CurrentUser = Annotated[str, Depends(get_current_user)]
