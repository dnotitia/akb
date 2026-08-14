"""Legacy app adoption plan, status, and metadata-only apply routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Request, Response, status

from app.api.control_plane_models import (
    LegacyAdoptionCreateRequest,
    LegacyAdoptionProjection,
)
from app.api.deps import get_current_user, request_correlation_id
from app.services.app_legacy_adoption_service import (
    apply_legacy_adoption,
    create_legacy_adoption,
    get_legacy_adoption,
)
from app.services.auth_service import AuthenticatedUser

router = APIRouter()


def _mark_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


@router.post(
    "/apps/{app_id}/legacy-adoptions",
    response_model=LegacyAdoptionProjection,
    operation_id="appsCreateLegacyAdoption",
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": LegacyAdoptionProjection}},
    summary="Create or replay an immutable legacy adoption preflight plan",
    description=(
        "The UUID Idempotency-Key and canonical app/release/target input identify "
        "an immutable metadata-only adoption plan. System administrators or all "
        "target Vault administrators may create it; table rows and physical "
        "schema are never changed by the dry-run."
    ),
)
async def create_adoption(
    app_id: uuid.UUID,
    req: LegacyAdoptionCreateRequest,
    request: Request,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    user: AuthenticatedUser = Depends(get_current_user),
):
    result = await create_legacy_adoption(
        app_id,
        baseline_release_id=req.baseline_release_id,
        idempotency_key=idempotency_key,
        targets=[target.model_dump() for target in req.targets],
        user=user,
        correlation_id=request_correlation_id(request),
    )
    _mark_no_store(response)
    if result.get("replayed"):
        response.status_code = status.HTTP_200_OK
    return result


@router.get(
    "/apps/{app_id}/legacy-adoptions/{adoption_id}",
    response_model=LegacyAdoptionProjection,
    operation_id="appsGetLegacyAdoption",
    summary="Read an immutable legacy adoption plan and target checkpoints",
)
async def get_adoption(
    app_id: uuid.UUID,
    adoption_id: uuid.UUID,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
):
    result = await get_legacy_adoption(
        app_id,
        adoption_id,
        user=user,
        correlation_id=request_correlation_id(request),
    )
    _mark_no_store(response)
    return result

@router.post(
    "/apps/{app_id}/legacy-adoptions/{adoption_id}/apply",
    response_model=LegacyAdoptionProjection,
    operation_id="appsApplyLegacyAdoption",
    summary="Apply or resume a legacy adoption plan",
)
async def apply_adoption(
    app_id: uuid.UUID,
    adoption_id: uuid.UUID,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
):
    result = await apply_legacy_adoption(
        app_id,
        adoption_id,
        user=user,
        correlation_id=request_correlation_id(request),
    )
    _mark_no_store(response)
    return result
