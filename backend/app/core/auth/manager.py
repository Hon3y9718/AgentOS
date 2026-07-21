"""fastapi-users UserManager — the customization seam (API_CONTRACT.md §1).

Role: password policy, string-id passthrough, and a PII-safe post-register
hook. Everything here is fastapi-users' extension point, not hand-rolled auth
logic — see app/core/auth/README.md for why this package exists outside
app/services/.
Called by: app/core/auth/users.py (get_user_manager dependency). Calls
app.config, app.models.user.
Gotcha: validate_password() is the *only* password policy enforced anywhere —
fastapi-users itself enforces none by default, so removing this override
silently accepts any non-empty password.
See: docs/DECISIONS/0003 Auth Layering.md
"""

from typing import Any

import structlog
from fastapi_users import BaseUserManager, InvalidPasswordException

from app.config import settings
from app.models.user import User

logger = structlog.get_logger()

# WHY 8, not a stronger policy (breach lists, complexity rules): a floor, not
# a real security requirement — this MVP had no password concept at all
# before this feature. Revisit if real-world signup traffic needs it.
_MIN_PASSWORD_LENGTH = 8


class UserManager(BaseUserManager[User, str]):
    # WHY the same secret for both: reset-password and email-verification
    # flows aren't exposed by any endpoint yet (no router calls
    # get_reset_password_router/get_verify_router), but BaseUserManager
    # requires these class attributes to exist regardless — see
    # app/config.py's secret_key docstring for the full tradeoff.
    reset_password_token_secret = settings.secret_key
    verification_token_secret = settings.secret_key

    def parse_id(self, value: Any) -> str:
        """Our user ids are already plain strings (core/ids.py's `user_...`
        format) — no UUID parsing/coercion needed, unlike fastapi-users'
        default UUID-based ID type (SQLAlchemyBaseUserTableUUID)."""
        return str(value)

    async def validate_password(self, password: str, user: Any) -> None:
        if len(password) < _MIN_PASSWORD_LENGTH:
            raise InvalidPasswordException(
                reason=f"Password must be at least {_MIN_PASSWORD_LENGTH} characters."
            )

    async def on_after_register(self, user: User, request: Any = None) -> None:
        # WHY user.id only, never user.email: CLAUDE.md — never log message
        # content, tokens, or prompts; the same instinct extends to a user's
        # email, which is PII this repo has no other reason to put in logs.
        logger.info("user_registered", user_id=user.id)
