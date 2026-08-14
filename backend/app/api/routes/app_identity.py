"""System-admin app credential lifecycle and app-token policy seam."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app.api.control_plane_models import (
    AuthorizeProjection,
    AuthorizeRequest,
    CredentialExchangeRequest,
    CredentialExchangeProjection,
    CredentialIssueRequest,
    CredentialIssueProjection,
    CredentialListProjection,
    CredentialMetadata,
    CredentialRotateRequest,
)

from app.api.deps import get_current_app, get_current_user, request_correlation_id
from app.services.app_identity_service import (
    AppPrincipal,
    authorize_app_request,
    exchange_app_credential,
    issue_app_credential,
    list_app_credentials,
    record_app_audit,
    revoke_app_credential,
    rotate_app_credential,
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


@router.post(
    "/apps/{app_id}/credentials",
    response_model=CredentialIssueProjection,
    operation_id="appsIssueCredential",
    summary="Issue an app deployment credential",
)
async def issue_credential(
    app_id: uuid.UUID,
    req: CredentialIssueRequest,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_system_admin(
        request,
        user,
        app_id=app_id,
        action="app.credential.issue",
    )
    result = await issue_app_credential(
        app_id,
        req.deployment,
        actor=user.username,
        actor_id=user.user_id,
        correlation_id=request_correlation_id(request),
        expires_at=req.expires_at,
    )
    _mark_no_store(response)
    return result


@router.get(
    "/apps/{app_id}/credentials",
    response_model=CredentialListProjection,
    operation_id="appsListCredentials",
    summary="List app credential metadata",
)
async def list_credentials(
    app_id: uuid.UUID,
    request: Request,
    response: Response,
    deployment: str | None = Query(default=None, max_length=128),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_system_admin(
        request,
        user,
        app_id=app_id,
        action="app.credential.list",
    )
    result = {"credentials": await list_app_credentials(app_id, deployment=deployment)}
    _mark_no_store(response)
    return result


@router.post(
    "/apps/{app_id}/credentials/{credential_id}/rotate",
    response_model=CredentialIssueProjection,
    operation_id="appsRotateCredential",
    summary="Rotate an app deployment credential",
)
async def rotate_credential(
    app_id: uuid.UUID,
    credential_id: uuid.UUID,
    req: CredentialRotateRequest,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_system_admin(
        request,
        user,
        app_id=app_id,
        action="app.credential.rotate",
    )
    result = await rotate_app_credential(
        app_id,
        credential_id,
        actor=user.username,
        actor_id=user.user_id,
        correlation_id=request_correlation_id(request),
        expires_at=req.expires_at,
    )
    _mark_no_store(response)
    return result


@router.delete(
    "/apps/{app_id}/credentials/{credential_id}",
    response_model=CredentialMetadata,
    operation_id="appsRevokeCredential",
    summary="Revoke an app deployment credential",
)
async def revoke_credential(
    app_id: uuid.UUID,
    credential_id: uuid.UUID,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_system_admin(
        request,
        user,
        app_id=app_id,
        action="app.credential.revoke",
    )
    result = await revoke_app_credential(
        app_id,
        credential_id,
        actor=user.username,
        actor_id=user.user_id,
        correlation_id=request_correlation_id(request),
    )
    _mark_no_store(response)
    return result


@router.post(
    "/auth/app-token",
    response_model=CredentialExchangeProjection,
    operation_id="authExchangeAppCredential",
    summary="Exchange an app credential for an app token",
)
async def exchange_credential(
    req: CredentialExchangeRequest,
    request: Request,
    response: Response,
):
    result = await exchange_app_credential(
        req.credential,
        correlation_id=request_correlation_id(request),
    )
    _mark_no_store(response)
    return result


@router.post(
    "/app/authorize",
    response_model=AuthorizeProjection,
    operation_id="appAuthorize",
    summary="Evaluate an app control-plane capability against the live registry",
)
async def authorize_app(
    req: AuthorizeRequest,
    request: Request,
    principal: AppPrincipal = Depends(get_current_app),
):
    correlation_id = request_correlation_id(request)
    await authorize_app_request(
        principal,
        vault_id=req.vault_id,
        capability=req.capability,
        correlation_id=correlation_id,
        resource_kind=req.resource_kind,
        resource_key=req.resource_key,
    )
    return {"authorized": True, "correlation_id": correlation_id}
