"""JWT authentication backend (API_CONTRACT.md §1).

Role: wires fastapi-users' Bearer transport + JWT strategy into one named
AuthenticationBackend, used by both the login/logout router
(app/api/v1/auth.py) and the CurrentUser dependency (app/api/v1/deps.py).
Called by: app/core/auth/users.py, app/api/v1/auth.py. Calls app.config.
Gotcha: this is a stateless strategy — a token is valid until it expires,
full stop. There is no server-side revocation list, so `/auth/logout` is a
client-side no-op against this backend (it only matters for cookie-based
transports, which we don't use). See docs/DECISIONS/0003 Auth Layering.md.
See: docs/API_CONTRACT.md#1-authentication
"""

from fastapi_users.authentication import AuthenticationBackend, BearerTransport, JWTStrategy

from app.config import settings
from app.models.user import User

# WHY 3600 (1h), not a module-level Settings field: this is a policy
# constant, not environment-varying config — same reasoning as chat.py's
# PING_INTERVAL_SECONDS. Short enough that a stolen token has a bounded
# blast radius given there's no revocation list (see module docstring).
_ACCESS_TOKEN_LIFETIME_SECONDS = 3600

bearer_transport = BearerTransport(tokenUrl="api/v1/auth/login")


def get_jwt_strategy() -> JWTStrategy[User, str]:
    return JWTStrategy(secret=settings.secret_key, lifetime_seconds=_ACCESS_TOKEN_LIFETIME_SECONDS)


auth_backend: AuthenticationBackend[User, str] = AuthenticationBackend(
    name="jwt",
    transport=bearer_transport,
    get_strategy=get_jwt_strategy,
)
