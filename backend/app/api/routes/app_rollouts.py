"""App release rollout request/status routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import Field

from app.api.deps import get_current_app, get_current_user, request_correlation_id
from app.services.app_identity_service import AppPrincipal
from app.services.app_rollout_service import (
    get_rollout,
    get_rollout_as_app,
    request_rollout_as_admin,
    request_rollout_as_app,
)
from app.services.auth_service import AuthenticatedUser
from app.util.text import NFCModel

router = APIRouter()


class RolloutRequest(NFCModel):
    release_id: uuid.UUID
    manifest_checksum: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")


def _mark_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


@router.post("/apps/{app_id}/rollouts", summary="Request an immutable staged app release rollout")
async def admin_request_rollout(
    app_id: uuid.UUID,
    req: RolloutRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: AuthenticatedUser = Depends(get_current_user),
):
    result = await request_rollout_as_admin(
        app_id,
        release_id=req.release_id,
        manifest_checksum_value=req.manifest_checksum,
        idempotency_key=idempotency_key or "",
        user=user,
        correlation_id=request_correlation_id(request),
    )
    _mark_no_store(response)
    response.status_code = status.HTTP_200_OK if result.get("replayed") else status.HTTP_202_ACCEPTED
    return result


@router.post("/app/rollouts", summary="Request a staged rollout for the caller app")
async def app_request_rollout(
    req: RolloutRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: AppPrincipal = Depends(get_current_app),
):
    result = await request_rollout_as_app(
        principal,
        release_id=req.release_id,
        manifest_checksum_value=req.manifest_checksum,
        idempotency_key=idempotency_key or "",
        correlation_id=request_correlation_id(request),
    )
    _mark_no_store(response)
    response.status_code = status.HTTP_200_OK if result.get("replayed") else status.HTTP_202_ACCEPTED
    return result


@router.get("/apps/{app_id}/rollouts/{rollout_id}", summary="Read an app rollout as a system administrator")
async def admin_get_rollout(
    app_id: uuid.UUID,
    rollout_id: uuid.UUID,
    response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
):
    if not user.is_admin:
        # Do this before the scoped lookup so a random id cannot probe app
        # existence through a non-admin status code or payload.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="System administrator permission required")
    result = await get_rollout(app_id, rollout_id)
    _mark_no_store(response)
    return result


@router.get("/app/rollouts/{rollout_id}", summary="Read a rollout for the caller app")
async def app_get_rollout(
    rollout_id: uuid.UUID,
    request: Request,
    response: Response,
    principal: AppPrincipal = Depends(get_current_app),
):
    result = await get_rollout_as_app(principal, rollout_id, correlation_id=request_correlation_id(request))
    _mark_no_store(response)
    return result
