"""Trusted Native provisioning for one non-admin operation requester.

The operator retains the credential before calling this module. Its ledger is
append-only and survives deletion; replay checks the same identity and value
without silently restoring a retired key. No HTTP issuer calls this module.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import stat
import uuid
from typing import NoReturn

import asyncpg

from app.db.postgres import get_pool
from app.repositories.events_repo import emit_event
from app.services.role_authority import role_authority_transaction
from app.services.role_sync import RoleSync
from app.sso.operation_request_scope import SSO_OPERATION_REQUEST_SCOPE

USERNAME = "sso-operation-requester"
EMAIL = "sso-operation-requester@service.invalid"
SENTINEL = "!service-account:no-local-login!"
SCOPES = [SSO_OPERATION_REQUEST_SCOPE]


class BootstrapRefused(Exception):
    """Value-free error safe for the CLI."""


def _refuse(code: str) -> NoReturn:
    raise BootstrapRefused(code)


def read_pipe(fd: int) -> str:
    """Accept a single credential only from an already-open pipe."""
    try:
        if not stat.S_ISFIFO(os.fstat(fd).st_mode):
            _refuse("approval_bootstrap_pipe_required")
        raw = b""
        while len(raw) <= 55:
            chunk = os.read(fd, 56 - len(raw))
            if not chunk:
                break
            raw += chunk
        if raw.endswith(b"\n"):
            raw = raw[:-1]
        value = raw.decode("ascii")
        if not re.fullmatch(r"akb_secret_[A-Za-z0-9_-]{43}", value):
            _refuse("approval_bootstrap_invalid_credential")
        decoded = base64.urlsafe_b64decode(value[11:] + "=")
        if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).decode().rstrip("=") != value[11:]:
            _refuse("approval_bootstrap_invalid_credential")
        return value
    except (OSError, UnicodeError, ValueError):
        _refuse("approval_bootstrap_invalid_credential")


async def provision(token_id: uuid.UUID, credential: str) -> dict:
    """Create or replay an exact token binding without rotating existing keys."""
    if not isinstance(token_id, uuid.UUID) or token_id.int == 0:
        _refuse("approval_bootstrap_invalid_token_id")
    # Also validate direct internal callers; the CLI's pipe reader validates too.
    if not re.fullmatch(r"akb_secret_[A-Za-z0-9_-]{43}", credential):
        _refuse("approval_bootstrap_invalid_credential")
    try:
        decoded = base64.urlsafe_b64decode(credential[11:] + "=")
    except ValueError:
        _refuse("approval_bootstrap_invalid_credential")
    if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).decode().rstrip("=") != credential[11:]:
        _refuse("approval_bootstrap_invalid_credential")
    digest = hashlib.sha256(credential.encode("ascii")).hexdigest()
    pool = await get_pool()
    roles = RoleSync(pool)
    try:
        async with pool.acquire() as conn, role_authority_transaction(conn):
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", USERNAME)
            installation = await conn.fetchval(
                "SELECT installation_id FROM sso_operation_installation WHERE singleton FOR SHARE"
            )
            if installation is None:
                _refuse("approval_bootstrap_installation_missing")
            bindings = await conn.fetch("SELECT DISTINCT user_id,installation_id FROM approval_service_bootstrap")
            if any(row["installation_id"] != installation for row in bindings) or len(bindings) > 1:
                _refuse("approval_bootstrap_lineage_conflict")
            if not bindings:
                if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM users WHERE username=$1 OR email=$2)", USERNAME, EMAIL):
                    _refuse("approval_bootstrap_account_exists")
                user_id = uuid.uuid4()
                await conn.execute("""INSERT INTO users(id,username,email,password_hash,display_name,
                    is_admin,auth_provider,account_status,account_kind)
                    VALUES($1,$2,$3,$4,'SSO operation requester',false,'service','active','service')""",
                    user_id, USERNAME, EMAIL, SENTINEL)
                await roles.on_user_create_in_conn(conn, user_id)
            else:
                user_id = bindings[0]["user_id"]
                user = await conn.fetchrow("""SELECT u.*,
                    EXISTS(SELECT 1 FROM external_identities e WHERE e.user_id=u.id) AS has_external,
                    EXISTS(SELECT 1 FROM service_identities s WHERE s.user_id=u.id) AS has_service_identity
                    FROM users u WHERE id=$1 FOR UPDATE""", user_id)
                if (user is None or user["username"] != USERNAME or user["email"] != EMAIL
                        or user["is_admin"] or user["is_recovery_admin"] or user["auth_provider"] != "service"
                        or user["account_status"] != "active" or user["account_kind"] != "service"
                        or user["has_external"] or user["has_service_identity"] or user["password_hash"] != SENTINEL
                        or user["credential_change_required"]):
                    _refuse("approval_bootstrap_account_changed")
                if not await roles.user_role_projected_in_conn(conn, user_id):
                    _refuse("approval_bootstrap_role_projection_changed")

            binding = await conn.fetchrow(
                "SELECT * FROM approval_service_bootstrap WHERE token_id=$1 FOR UPDATE", token_id
            )
            if binding is not None:
                if (binding["user_id"] != user_id or binding["installation_id"] != installation
                        or binding["credential_sha256"] != digest):
                    _refuse("approval_bootstrap_credential_conflict")
                token = await conn.fetchrow("SELECT * FROM tokens WHERE id=$1 FOR UPDATE", token_id)
                if (token is None or token["user_id"] != user_id or token["name"] != USERNAME
                        or token["token_hash"] != digest or token["token_prefix"] != credential[:12]
                        or token["key_class"] != "service" or token["scopes"] != SCOPES
                        or token["vault_scope"] is not None or token["expires_at"] is not None):
                    _refuse("approval_bootstrap_credential_retired")
                result = "replayed"
            else:
                await conn.execute("""INSERT INTO tokens(id,user_id,name,token_hash,token_prefix,scopes,key_class)
                    VALUES($1,$2,$3,$4,$5,$6,'service')""",
                    token_id, user_id, USERNAME, digest, credential[:12], SCOPES)
                await conn.execute("""INSERT INTO approval_service_bootstrap(token_id,user_id,installation_id,credential_sha256)
                    VALUES($1,$2,$3,$4)""", token_id, user_id, installation, digest)
                await emit_event(conn, "auth.sso_approval_service_credential_issued", actor_id=str(user_id),
                                 payload={"user_id": str(user_id), "token_id": str(token_id),
                                          "installation_id": str(installation), "scopes": SCOPES})
                result = "issued"
    except asyncpg.UniqueViolationError:
        _refuse("approval_bootstrap_identity_conflict")
    return {"schema_version": 1, "status": result, "requester_user_id": str(user_id),
            "requester_token_id": str(token_id), "installation_id": str(installation),
            "key_class": "service", "scopes": list(SCOPES), "vault_scope": None}
