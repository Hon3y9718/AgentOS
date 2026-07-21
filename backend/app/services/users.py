"""Per-user token usage quota (API_CONTRACT.md §1). No fastapi import
(ARCHITECTURE.md's layering rule) — unlike app/core/auth/, this is genuine
domain logic, not fastapi-users wiring, so it lives here like every other
service.
Called by: app/services/chat.py. Calls app.models.user, app.core.errors.
Gotcha: increment_tokens_used() is an atomic SQL UPDATE, not a Python
read-modify-write — two concurrent turns for the same user must not lose an
increment to a lost-update race (SELECT, add in Python, UPDATE would).
See: docs/DECISIONS/0003 Auth Layering.md
"""

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import UsageLimitExceededError
from app.models.user import User as UserModel


async def check_usage_limit(db: AsyncSession, user_id: str) -> None:
    """Raise if `user_id` has already used up its token quota.

    Raises:
        UsageLimitExceededError: `tokens_used >= token_limit`.
    """
    row = (
        await db.execute(
            select(UserModel.tokens_used, UserModel.token_limit).where(UserModel.id == user_id)
        )
    ).one()
    if row.tokens_used >= row.token_limit:
        raise UsageLimitExceededError(
            "Token usage limit reached for this account.",
            code="usage.limit_exceeded",
            details={"tokens_used": row.tokens_used, "token_limit": row.token_limit},
        )


async def increment_tokens_used(db: AsyncSession, user_id: str, tokens: int) -> None:
    """Add `tokens` to `user_id`'s running total.

    WHY this doesn't commit: called from within app/services/chat.py's own
    turn-finalizing commit, same pattern as chat.py's _bump_conversation().
    """
    await db.execute(
        update(UserModel)
        .where(UserModel.id == user_id)
        .values(tokens_used=UserModel.tokens_used + tokens)
    )
