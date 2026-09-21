"""Authorize every public mint path; PAT credentials cannot mint children."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING
import uuid

from app.config import settings
from app.exceptions import AKBError

if TYPE_CHECKING:
    from app.services.auth_service import AuthenticatedUser


def require_issuer_carrier(issuer: AuthenticatedUser, *, admin_route: bool = False) -> None:
    if issuer.account_kind == "human" and issuer.auth_method in {"jwt", "oauth", "browser_session"}:
        if not admin_route or issuer.is_admin:
            return
    if (admin_route and issuer.is_admin and issuer.auth_method == "pat" and issuer.key_class == "service"
            and issuer.token_id is not None and uuid.UUID(issuer.token_id) in settings.admin_token_issuer_ids
            and issuer.vault_scope is None and issuer.token_scopes is not None
            and ("admin" in issuer.token_scopes or {"read", "write"}.issubset(issuer.token_scopes))):
        return
    raise AKBError("A human session is required to issue tokens.", 403, code="human_token_issuer_required")


async def validate_locked_issuer(conn, issuer: AuthenticatedUser, row, *, admin_route: bool, key_class: str) -> None:
    """User rows are already locked in UUID order by the mint transaction."""
    require_issuer_carrier(issuer, admin_route=admin_route)
    if row is None or row["account_status"] != "active":
        raise AKBError("The token issuer is no longer active.", 403, code="token_issuer_inactive")
    if (admin_route or key_class != "pat") and not row["is_admin"]:
        raise AKBError("Administrator authorization is required.", 403, code="token_issuer_admin_required")
    if issuer.auth_method in {"jwt", "oauth", "browser_session"}:
        provider = "local" if issuer.auth_method == "jwt" else "keycloak"
        if (row["account_kind"] != "human" or row["auth_provider"] != provider
                or (issuer.auth_method == "jwt" and row["credential_change_required"])):
            raise AKBError("The human account changed. Sign in again.", 403, code="human_token_issuer_required")
        return
    token = await conn.fetchrow("SELECT * FROM tokens WHERE id=$1 FOR SHARE", uuid.UUID(issuer.token_id))
    scopes = set(token["scopes"] or []) if token else set()
    if (token is None or token["user_id"] != row["id"] or token["key_class"] != "service"
            or token["vault_scope"] is not None or uuid.UUID(issuer.token_id) not in settings.admin_token_issuer_ids
            or ("admin" not in scopes and not {"read", "write"}.issubset(scopes))
            or (token["expires_at"] is not None and token["expires_at"] <= datetime.now(timezone.utc))):
        raise AKBError("The approved machine issuer is no longer valid.", 403, code="machine_token_issuer_invalid")
