"""Shared authorization context for File and document-image writes."""

from __future__ import annotations

from fastapi import Request

from app.api.deps import require_delegated_actor
from app.services.access_service import (
    FILE_UPLOAD_WRITE_ACTION,
    check_delegated_vault_writer,
    check_vault_access,
)
from app.services.auth_service import AuthenticatedUser


async def resolve_file_write_context(
    request: Request,
    vault: str,
    user: AuthenticatedUser,
) -> tuple[dict, str, dict[str, str] | None]:
    """Apply the same writer/delegation policy to every byte-upload route."""
    access = await check_vault_access(
        user.user_id,
        vault,
        required_role="writer",
        write_action=FILE_UPLOAD_WRITE_ACTION,
    )
    actions = frozenset(access.get("write_grant_actions") or [])
    if access.get("role_source") != "write_policy_grant" or "*" in actions:
        return access, user.username, None

    delegated_actor = await require_delegated_actor(request, user)
    await check_delegated_vault_writer(delegated_actor.user.user_id, vault)
    return access, delegated_actor.user.username, {
        "delegated_user_id": delegated_actor.user.user_id,
        "service_user_id": delegated_actor.service_user_id,
        "service_token_id": delegated_actor.service_token_id,
    }
