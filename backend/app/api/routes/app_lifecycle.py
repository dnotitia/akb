"""REST lifecycle commands and status for app installations."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field

from app.api.deps import get_current_app, get_current_user, request_correlation_id
from app.services.app_identity_service import AppPrincipal
from app.services.app_lifecycle_service import (
    authorize_lifecycle_admin,
    get_installation_status,
    get_installation_status_for_app,
    put_installation,
    uninstall_installation,
)
from app.services.auth_service import AuthenticatedUser
from app.util.text import NFCModel

router = APIRouter()

LifecycleMode = Literal["install", "restore", "fresh"]
CapabilityName = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
]


class InstallationCommandRequest(NFCModel):
    release_id: uuid.UUID
    capabilities: list[CapabilityName] = Field(min_length=1, max_length=32)
    mode: LifecycleMode = "install"


def _mark_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _set_command_status(response: Response, result: dict) -> None:
    response.status_code = 200 if result["replayed"] else 202
    _mark_no_store(response)


@router.put(
    "/apps/{app_id}/installations/{vault_id}",
    summary="Install, restore, or freshly provision an app in a Vault",
)
async def put_app_installation(
    app_id: uuid.UUID,
    vault_id: uuid.UUID,
    req: InstallationCommandRequest,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
):
    correlation_id = request_correlation_id(request)
    await authorize_lifecycle_admin(
        user,
        app_id=app_id,
        vault_id=vault_id,
        action="app.installation.command",
        correlation_id=correlation_id,
    )
    result = await put_installation(
        app_id,
        vault_id,
        release_id=req.release_id,
        capabilities=req.capabilities,
        mode=req.mode,
        correlation_id=correlation_id,
        actor=user.username,
        actor_id=user.user_id,
    )
    _set_command_status(response, result)
    return result


@router.get(
    "/apps/{app_id}/installations/{vault_id}",
    summary="Read an app installation status as a system or Vault administrator",
)
async def get_app_installation(
    app_id: uuid.UUID,
    vault_id: uuid.UUID,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
):
    correlation_id = request_correlation_id(request)
    await authorize_lifecycle_admin(
        user,
        app_id=app_id,
        vault_id=vault_id,
        action="app.installation.status",
        correlation_id=correlation_id,
    )
    result = await get_installation_status(app_id, vault_id)
    _mark_no_store(response)
    return result


@router.delete(
    "/apps/{app_id}/installations/{vault_id}",
    summary="Uninstall an app from a Vault without deleting retained resources",
)
async def delete_app_installation(
    app_id: uuid.UUID,
    vault_id: uuid.UUID,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
):
    correlation_id = request_correlation_id(request)
    await authorize_lifecycle_admin(
        user,
        app_id=app_id,
        vault_id=vault_id,
        action="app.installation.uninstall",
        correlation_id=correlation_id,
    )
    result = await uninstall_installation(
        app_id,
        vault_id,
        correlation_id=correlation_id,
        actor=user.username,
        actor_id=user.user_id,
    )
    _set_command_status(response, result)
    return result


@router.get(
    "/app/installations/{vault_id}",
    summary="Read the caller app's own installation status",
)
async def get_caller_app_installation(
    vault_id: uuid.UUID,
    request: Request,
    response: Response,
    principal: AppPrincipal = Depends(get_current_app),
):
    result = await get_installation_status_for_app(
        principal,
        vault_id=vault_id,
        correlation_id=request_correlation_id(request),
    )
    _mark_no_store(response)
    return result
