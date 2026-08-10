"""REST app installation lifecycle command and status surfaces."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import Field

from app.api.deps import get_current_app, get_current_user, request_correlation_id
from app.services.app_identity_service import AppPrincipal
from app.services.app_installation_service import (
    command_installation,
    get_admin_installation_status,
    get_app_installation_status,
    uninstall_installation,
)
from app.services.auth_service import AuthenticatedUser
from app.util.text import NFCModel

router = APIRouter()


class InstallationCommandRequest(NFCModel):
    release_id: uuid.UUID
    capabilities: list[str] = Field(min_length=1, max_length=32)
    mode: Literal["install", "restore", "fresh"] = "install"


def _mark_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


@router.put(
    "/apps/{app_id}/installations/{vault_id}",
    summary="Install, restore, or freshly install an app in a Vault",
)
async def put_installation(
    app_id: uuid.UUID,
    vault_id: uuid.UUID,
    req: InstallationCommandRequest,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
):
    result = await command_installation(
        app_id,
        vault_id,
        release_id=req.release_id,
        capabilities=req.capabilities,
        mode=req.mode,
        user=user,
        correlation_id=request_correlation_id(request),
    )
    _mark_no_store(response)
    if result["replayed"]:
        response.status_code = status.HTTP_200_OK
    else:
        response.status_code = status.HTTP_202_ACCEPTED
    return result

@router.get(
    "/apps/{app_id}/installations/{vault_id}",
    summary="Read an app installation as a system or Vault administrator",
)
async def get_installation(
    app_id: uuid.UUID,
    vault_id: uuid.UUID,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
):
    result = await get_admin_installation_status(
        app_id,
        vault_id,
        user=user,
        correlation_id=request_correlation_id(request),
    )
    _mark_no_store(response)
    return result


@router.delete(
    "/apps/{app_id}/installations/{vault_id}",
    summary="Uninstall an app from a Vault while retaining owned resources",
)
async def delete_installation(
    app_id: uuid.UUID,
    vault_id: uuid.UUID,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
):
    result = await uninstall_installation(
        app_id,
        vault_id,
        user=user,
        correlation_id=request_correlation_id(request),
    )
    _mark_no_store(response)
    response.status_code = (
        status.HTTP_200_OK if result["replayed"] else status.HTTP_202_ACCEPTED
    )
    return result


@router.get(
    "/app/installations/{vault_id}",
    summary="Read the calling app's installation in a Vault",
)
async def get_app_installation(
    vault_id: uuid.UUID,
    request: Request,
    response: Response,
    principal: AppPrincipal = Depends(get_current_app),
):
    result = await get_app_installation_status(
        principal,
        vault_id,
        correlation_id=request_correlation_id(request),
    )
    _mark_no_store(response)
    return result
