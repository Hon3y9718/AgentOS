"""User schemas (API_CONTRACT.md §1).

Role: wire shapes for register/login responses. Extends fastapi_users'
schemas.BaseUser/BaseUserCreate rather than defining these from scratch,
since fastapi-users' own routers (app/api/v1/auth.py) construct and consume
them directly — unlike every other schema in this package, these are not
free-standing.
Called by: app/api/v1/auth.py, app/core/auth/users.py (via FastAPIUsers[User, str]).
Calls nothing internal.
Gotcha: sending `is_active`/`is_superuser`/`is_verified` in a register request
body is accepted by validation (they're real fields on BaseUserCreate) but
always ignored server-side — fastapi-users' register route calls
`UserManager.create(..., safe=True)`, which strips exactly those three fields
before insert (see fastapi_users.schemas.CreateUpdateDictModel.create_update_dict).
Not a privilege-escalation hole; just a mildly confusing schema shape.
See: docs/API_CONTRACT.md#1-authentication
"""

from fastapi_users import schemas
from pydantic import ConfigDict


class UserRead(schemas.BaseUser[str]):
    # WHY from_attributes=True is kept (not just extra="forbid" like every
    # other schema in this package): the register router builds this via
    # `UserRead.model_validate(created_user)` where created_user is the ORM
    # User row, not a dict — losing from_attributes here would break that call.
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    token_limit: int
    tokens_used: int


class UserCreate(schemas.BaseUserCreate):
    # WHY extra="forbid" even though fastapi-users' own base doesn't set it:
    # matches this repo's request-schema convention (API_CONTRACT §0 —
    # "Unknown fields in a request are rejected").
    model_config = ConfigDict(extra="forbid")
