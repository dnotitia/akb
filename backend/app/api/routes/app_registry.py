"""System-admin app definition and immutable release registry routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.api.control_plane_models import (
    AppCreateRequest,
    AppDefinitionProjection,
    AppReleaseProjection,
    AppUpdateRequest,
    ReleaseCreateRequest,
)
from app.api.deps import get_current_user, request_correlation_id
from app.services.app_identity_service import record_app_audit
from app.services.app_registry_service import (
    create_app_definition,
    create_app_release,
    get_app_definition,
    get_app_release,
    update_app_definition,
)
from app.services.auth_service import AuthenticatedUser

router = APIRouter()


def _mark_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _require_system_admin(
    request: Request,
    user: AuthenticatedUser,
    *,
    action: str,
    app_id: uuid.UUID | None = None,
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


@router.post(
    "/apps",
    response_model=AppDefinitionProjection,
    operation_id="appsCreate",
    summary="Create or replay a generic app definition",
    description=(
        "Natural-key idempotency is authoritative: app_key identifies the app. "
        "An identical body replays the existing projection with replayed=true; "
        "a different body for the same app_key returns 409. No Idempotency-Key "
        "header is used for this registry operation."
    ),
)
async def create_app(
    req: AppCreateRequest,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_system_admin(request, user, action="app.registry.create")
    result = await create_app_definition(
        app_key=req.app_key,
        display_name=req.display_name,
        description=req.description,
        metadata=req.metadata,
        user=user,
        correlation_id=request_correlation_id(request),
    )
    _mark_no_store(response)
    return result


@router.get(
    "/apps/{app_id}",
    response_model=AppDefinitionProjection,
    operation_id="appsGet",
    summary="Read an app definition",
)
async def get_app(
    app_id: uuid.UUID,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_system_admin(request, user, action="app.registry.read", app_id=app_id)
    result = await get_app_definition(
        app_id, user=user, correlation_id=request_correlation_id(request)
    )
    _mark_no_store(response)
    return result


@router.patch(
    "/apps/{app_id}",
    response_model=AppDefinitionProjection,
    operation_id="appsUpdate",
    summary="Update mutable app display metadata",
)
async def update_app(
    app_id: uuid.UUID,
    req: AppUpdateRequest,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_system_admin(request, user, action="app.registry.update", app_id=app_id)
    result = await update_app_definition(
        app_id,
        fields=req.model_dump(exclude_unset=True),
        user=user,
        correlation_id=request_correlation_id(request),
    )
    _mark_no_store(response)
    return result


@router.post(
    "/apps/{app_id}/releases",
    response_model=AppReleaseProjection,
    operation_id="appsCreateRelease",
    summary="Create or replay an immutable app release",
    description=(
        "Natural-key idempotency is authoritative: (app_id, version) identifies "
        "the release. An identical manifest and checksum replay the existing "
        "projection with replayed=true; different content for the same version "
        "returns 409. No Idempotency-Key header is used for this registry "
        "operation. The manifest must be a strict App Release Manifest v2 with "
        "provenance, a complete desired schema projection, and source-specific "
        "transition plans."
    ),
)
async def create_release(
    app_id: uuid.UUID,
    req: ReleaseCreateRequest,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_system_admin(request, user, action="app.registry.release.create", app_id=app_id)
    result = await create_app_release(
        app_id,
        version=req.version,
        manifest=req.manifest.model_dump(exclude_none=True, by_alias=True),
        manifest_checksum=req.manifest_checksum,
        user=user,
        correlation_id=request_correlation_id(request),
    )
    _mark_no_store(response)
    return result


@router.get(
    "/apps/{app_id}/releases/{release_id}",
    response_model=AppReleaseProjection,
    operation_id="appsGetRelease",
    summary="Read an immutable app release",
)
async def get_release(
    app_id: uuid.UUID,
    release_id: uuid.UUID,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_system_admin(request, user, action="app.registry.release.read", app_id=app_id)
    result = await get_app_release(
        app_id,
        release_id,
        user=user,
        correlation_id=request_correlation_id(request),
    )
    _mark_no_store(response)
    return result
