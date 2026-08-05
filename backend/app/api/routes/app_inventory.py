"""REST control-plane surface for app inventory and rollout snapshots."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import Field

from app.api.deps import get_current_app, get_current_user, request_correlation_id
from app.services.app_identity_service import (
    AppPrincipal,
    authorize_app_capability,
    record_app_audit,
)
from app.services.auth_service import AuthenticatedUser
from app.services.app_inventory_service import (
    create_rollout_snapshot,
    evaluate_rollout_target,
    get_rollout_snapshot,
    list_inventory,
    report_observed_state,
)
from app.util.text import NFCModel

router = APIRouter()


class ObservedStateRequest(NFCModel):
    installation_id: uuid.UUID
    observed_generation: int = Field(ge=0)
    observed_at: datetime | None = None
    observed_release_id: uuid.UUID | None = None
    observed_release_version: str | None = Field(default=None, max_length=256)
    schema_fingerprint: str | None = Field(default=None, max_length=256)
    observed_grant_generation: int | None = Field(default=None, ge=0)
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    recent_error: dict[str, Any] | None = None


class SnapshotRequest(NFCModel):
    """Reserved empty request body for future snapshot labels."""


def _mark_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _require_system_admin(
    request: Request,
    user: AuthenticatedUser,
    *,
    app_id: uuid.UUID,
    action: str,
) -> None:
    if user.is_admin:
        return
    record_app_audit(
        action,
        correlation_id=request_correlation_id(request),
        outcome="error",
        reason="system_admin_required",
        actor=user.username,
        actor_id=user.user_id,
        app_id=app_id,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="System administrator permission required",
    )


async def _authorize_app(
    principal: AppPrincipal,
    request: Request,
    capability: str,
) -> str:
    correlation_id = request_correlation_id(request)
    await authorize_app_capability(
        principal,
        capability=capability,
        correlation_id=correlation_id,
    )
    return correlation_id


@router.get(
    "/apps/{app_id}/inventory",
    summary="Read an app installation inventory as a system administrator",
)
async def admin_inventory(
    app_id: uuid.UUID,
    request: Request,
    response: Response,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=4096),
    lifecycle: str | None = Query(default=None, max_length=32),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_system_admin(
        request,
        user,
        app_id=app_id,
        action="app.inventory.read",
    )
    result = await list_inventory(
        app_id,
        limit=limit,
        cursor=cursor,
        lifecycle=lifecycle,
        scope="admin",
    )
    _mark_no_store(response)
    return result


@router.get(
    "/app/inventory",
    summary="Read the caller app's installation inventory",
)
async def app_inventory(
    request: Request,
    response: Response,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=4096),
    lifecycle: str | None = Query(default=None, max_length=32),
    principal: AppPrincipal = Depends(get_current_app),
):
    await _authorize_app(principal, request, "inventory:read")
    result = await list_inventory(
        principal.app_id,
        limit=limit,
        cursor=cursor,
        lifecycle=lifecycle,
        scope="app",
        capability="inventory:read",
    )
    _mark_no_store(response)
    return result


@router.post(
    "/apps/{app_id}/observed-state",
    summary="Record an app installation worker observation as a system administrator",
)
async def admin_observed_state(
    app_id: uuid.UUID,
    req: ObservedStateRequest,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_system_admin(
        request,
        user,
        app_id=app_id,
        action="app.inventory.observed",
    )
    result = await report_observed_state(
        req.installation_id,
        observed_generation=req.observed_generation,
        observed_at=req.observed_at,
        observed_release_id=req.observed_release_id,
        observed_release_version=req.observed_release_version,
        schema_fingerprint=req.schema_fingerprint,
        observed_grant_generation=req.observed_grant_generation,
        checkpoint=req.checkpoint,
        recent_error=req.recent_error,
        app_id=app_id,
        correlation_id=request_correlation_id(request),
        actor=user.username,
        actor_id=user.user_id,
    )
    _mark_no_store(response)
    return result


@router.post(
    "/app/observed-state",
    summary="Record the caller app's installation worker observation",
)
async def app_observed_state(
    req: ObservedStateRequest,
    request: Request,
    response: Response,
    principal: AppPrincipal = Depends(get_current_app),
):
    result = await report_observed_state(
        req.installation_id,
        observed_generation=req.observed_generation,
        observed_at=req.observed_at,
        observed_release_id=req.observed_release_id,
        observed_release_version=req.observed_release_version,
        schema_fingerprint=req.schema_fingerprint,
        observed_grant_generation=req.observed_grant_generation,
        checkpoint=req.checkpoint,
        recent_error=req.recent_error,
        principal=principal,
        correlation_id=request_correlation_id(request),
        actor=f"app:{principal.app_id}",
        actor_id=str(principal.app_id),
    )
    _mark_no_store(response)
    return result


@router.post(
    "/apps/{app_id}/rollout-snapshots",
    summary="Create an immutable rollout target snapshot as a system administrator",
)
async def admin_create_snapshot(
    app_id: uuid.UUID,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
    _req: SnapshotRequest | None = None,
):
    _require_system_admin(
        request,
        user,
        app_id=app_id,
        action="app.rollout.snapshot.create",
    )
    result = await create_rollout_snapshot(
        app_id,
        requested_by_kind="admin",
        correlation_id=request_correlation_id(request),
        actor=user.username,
        actor_id=user.user_id,
    )
    _mark_no_store(response)
    return result


@router.post(
    "/app/rollout-snapshots",
    summary="Create an immutable rollout target snapshot for the caller app",
)
async def app_create_snapshot(
    request: Request,
    response: Response,
    principal: AppPrincipal = Depends(get_current_app),
    _req: SnapshotRequest | None = None,
):
    correlation_id = await _authorize_app(principal, request, "rollout:request")
    result = await create_rollout_snapshot(
        principal.app_id,
        requested_by_kind="app",
        correlation_id=correlation_id,
        actor=f"app:{principal.app_id}",
        actor_id=str(principal.app_id),
    )
    _mark_no_store(response)
    return result


@router.get(
    "/apps/{app_id}/rollout-snapshots/{snapshot_id}",
    summary="Read an app rollout target snapshot as a system administrator",
)
async def admin_get_snapshot(
    app_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_system_admin(
        request,
        user,
        app_id=app_id,
        action="app.rollout.snapshot.read",
    )
    result = await get_rollout_snapshot(app_id, snapshot_id)
    _mark_no_store(response)
    return result


@router.get(
    "/app/rollout-snapshots/{snapshot_id}",
    summary="Read a caller-app rollout target snapshot",
)
async def app_get_snapshot(
    snapshot_id: uuid.UUID,
    request: Request,
    response: Response,
    principal: AppPrincipal = Depends(get_current_app),
):
    await _authorize_app(principal, request, "rollout:read")
    result = await get_rollout_snapshot(principal.app_id, snapshot_id)
    _mark_no_store(response)
    return result


@router.post(
    "/apps/{app_id}/rollout-snapshots/{snapshot_id}/targets/{target_id}/eligibility",
    summary="Recheck a rollout target before execution as a system administrator",
)
async def admin_evaluate_target(
    app_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    target_id: uuid.UUID,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_system_admin(
        request,
        user,
        app_id=app_id,
        action="app.rollout.target.eligibility",
    )
    result = await evaluate_rollout_target(
        app_id,
        snapshot_id,
        target_id,
        correlation_id=request_correlation_id(request),
        actor=user.username,
        actor_id=user.user_id,
    )
    _mark_no_store(response)
    return result


@router.post(
    "/app/rollout-snapshots/{snapshot_id}/targets/{target_id}/eligibility",
    summary="Recheck a caller-app rollout target before execution",
)
async def app_evaluate_target(
    snapshot_id: uuid.UUID,
    target_id: uuid.UUID,
    request: Request,
    response: Response,
    principal: AppPrincipal = Depends(get_current_app),
):
    correlation_id = await _authorize_app(principal, request, "rollout:request")
    result = await evaluate_rollout_target(
        principal.app_id,
        snapshot_id,
        target_id,
        correlation_id=correlation_id,
        actor=f"app:{principal.app_id}",
        actor_id=str(principal.app_id),
    )
    _mark_no_store(response)
    return result
