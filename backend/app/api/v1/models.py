"""Models router (API_CONTRACT.md §4).

Role: HTTP surface for GET /api/v1/models. Thin — validates query params,
calls app.services.models, returns the schema.
Called by: app/main.py (included under /api/v1). Calls app.services.models,
app.api.v1.deps.
Gotcha: authenticated like every other /api/v1 route (CurrentUser), even
though the registry isn't per-user data — §1 only carves out an
unauthenticated exception for /health and /health/ready, not this endpoint.
See: docs/API_CONTRACT.md#4-model-registry
"""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.v1.deps import CurrentUser
from app.schemas.model import ModelList
from app.services import models as service

router = APIRouter(prefix="/models", tags=["models"])

# WHY Annotated + a `None` default, not `= Query(default=[])`, only for this
# one param: FastAPI itself rejects setting a default inside `Query(...)`
# when the parameter is `Annotated` ("Set the default value with `=`
# instead") — and a bare `= []` default would trip ruff/bugbear's B006
# (mutable default argument). `None`, converted to `[]` in the function
# body below, is immutable and satisfies both.
# WHY `capability`, singular, as the query param name for a repeatable list:
# matches §4's own example verbatim (`?capability=tools`,
# `?capability=vision`), not the more common plural-param convention.
CapabilityFilter = Annotated[list[str] | None, Query()]


@router.get("", response_model=ModelList)
async def list_models(
    _user_id: CurrentUser,
    capability: CapabilityFilter = None,
    provider: str | None = Query(default=None),
    available: bool | None = Query(default=None),
) -> ModelList:
    return await service.list_models(
        provider=provider, capabilities=capability or [], available=available
    )
