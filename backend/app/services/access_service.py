"""Access control service — vault roles, grant/revoke, permission checks.

Role hierarchy: owner > admin > writer > reader > (none)
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from app.db.postgres import get_pool
from app.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RecoveryAdminProtectedError,
    ValidationError,
)
from app.models.vault_scope import VaultScope, current_token_uuid, current_vault_scope
from app.repositories import vault_write_policy_repo as write_policy_repo
from app.repositories.events_repo import emit_event
from app.repositories.vault_files_repo import confirmed_file_predicate
from app.services.account_markers import is_retired_recovery_admin_password
from app.services.role_sync import get_role_sync
from app.services.uri_service import vault_uri
from app.services.write_lane import run_compensation, run_git_write
from app.util.errors import NOT_FOUND, err

logger = logging.getLogger("akb.access")

ROLE_HIERARCHY = {"owner": 4, "admin": 3, "writer": 2, "reader": 1}
VALID_ROLES = set(ROLE_HIERARCHY.keys())
VALID_PUBLIC_ACCESS = {"none", "reader", "writer"}

# Roles that MUTATE a vault — the Option B per-PAT vault-scope guard fires only
# for these (reads are never scope-restricted: a scoped agent still reads
# broadly, the scope bounds WRITES).
_MUTATING_ROLES = frozenset({"writer", "admin", "owner"})

WRITE_ACTION_WILDCARD = "*"
FILE_UPLOAD_WRITE_ACTION = "file_upload"
VALID_WRITE_ACTIONS = frozenset({WRITE_ACTION_WILDCARD, FILE_UPLOAD_WRITE_ACTION})


def normalize_write_actions(actions: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """Validate the small managed-write capability vocabulary.

    ``None`` preserves the pre-045 broad-grant API. Mixing wildcard and named
    actions is rejected rather than silently widening a grant.
    """
    if actions is None:
        return (WRITE_ACTION_WILDCARD,)
    if not actions:
        raise ValidationError("write actions must not be empty")
    if any(not isinstance(action, str) or not action.strip() for action in actions):
        raise ValidationError("write actions must be non-empty strings")
    normalized = tuple(sorted({action.strip() for action in actions}))
    unknown = set(normalized) - VALID_WRITE_ACTIONS
    if unknown:
        raise ValidationError(
            f"Unknown write action(s): {sorted(unknown)}. "
            f"Use: {sorted(VALID_WRITE_ACTIONS)}"
        )
    if WRITE_ACTION_WILDCARD in normalized and len(normalized) > 1:
        raise ValidationError("wildcard write action cannot be combined with named actions")
    return normalized


def _validate_action_limited_grant_token(token: dict, vault_name: str) -> None:
    """Fail closed unless a limited grant targets an upload-only credential."""
    if token["key_class"] != "service":
        raise ValidationError("action-limited write grants require a service key")
    if token["account_kind"] != "service" or token["is_admin"]:
        raise ValidationError(
            "action-limited write grants require a non-admin service account"
        )
    if token["account_status"] != "active":
        raise ValidationError(
            "action-limited write grants require an active service account"
        )
    if not token["is_unexpired"]:
        raise ValidationError(
            "action-limited write grants require an unexpired service key"
        )

    scopes = frozenset(str(scope) for scope in (token["scopes"] or ()))
    if scopes != {"write"}:
        raise ValidationError(
            "action-limited write grants require exactly the coarse scope ['write']"
        )

    try:
        vault_scope = VaultScope.from_db_json(token["vault_scope"])
    except ValueError as exc:
        raise ValidationError(f"invalid token Vault scope: {exc}") from exc
    if (
        vault_scope is None
        or vault_scope.prefixes
        or vault_scope.extra_vaults != {vault_name}
    ):
        raise ValidationError(
            "action-limited write grants require a token scoped to exactly the target Vault"
        )


def _role_level(role: str) -> int:
    return ROLE_HIERARCHY.get(role, 0)


def validate_public_access(level: str) -> str:
    """Enum guard for vaults.public_access writes.

    The column used to accept any string, which let a typo like "write"
    slip in and break both RoleBadge rendering (unknown role key) and
    `check_vault_access` (role-level lookup returns 0, so public access
    silently fails).
    """
    if level not in VALID_PUBLIC_ACCESS:
        raise ValidationError(
            f"Invalid public_access '{level}'. "
            f"Must be one of: {sorted(VALID_PUBLIC_ACCESS)}"
        )
    return level


# ── Permission checks ───────────────────────────────────────

async def _is_unscoped_system_admin(uid: uuid.UUID, conn) -> bool:
    """True iff `uid` is a system admin AND this request's PAT (if any)
    carries no vault scope — the A3 bypass condition for the
    write-policy guard below.

    A *scoped* admin PAT must NOT get this bypass: same "a scope only
    ever subtracts authority" rule the Option B scope guard enforces a
    few lines up in `check_vault_access`. Re-queries `users.is_admin`
    (rather than reusing the one a few lines below) because this runs
    BEFORE that existing short-circuit computes it.
    """
    if current_vault_scope.get() is not None:
        return False
    return bool(await conn.fetchval("SELECT is_admin FROM users WHERE id = $1", uid))


def check_vault_scope(vault_name: str, required_role: str) -> None:
    """Option B per-PAT vault scope, as a NAME-only check.

    The scope is a pure name predicate — it needs no vault row — so this
    is the whole policy, and `check_vault_access` calls it for vaults
    that already exist. Split out so the two callers cannot drift: the
    denial message and the resulting `permission_denied` code are
    identical whichever surface refuses.

    Callers that have no row to resolve use it directly. `create_vault`
    is the motivating case (dnotitia/akb#284): creation is a mutating
    op on a name that does not exist yet, so it could not route through
    `check_vault_access`, and without this it silently escaped the
    scope — a scoped token could plant a vault anywhere in the
    namespace and was then denied every admin op on the vault it had
    just made, including deleting it. Creation passes
    `required_role="owner"` because creating a vault confers ownership
    of it.

    No-op when the request carries no scope (`None` ⇒ unscoped, the
    historical full-ACL behaviour) or when `required_role` is
    non-mutating — reads are never scope-restricted.
    """
    scope = current_vault_scope.get()
    if (
        scope is not None
        and required_role in _MUTATING_ROLES
        and not scope.permits(vault_name)
    ):
        raise ForbiddenError(
            f"Token scope does not permit '{required_role}' on vault '{vault_name}'"
        )


async def check_vault_access(
    user_id: str, vault_name: str, required_role: str = "reader",
    *, allow_archived: bool = False, write_action: str | None = None,
) -> dict:
    """Check if user has at least the required role on a vault.

    Returns vault info dict if authorized.
    Raises ForbiddenError if not.
    Raises NotFoundError if vault doesn't exist.

    `allow_archived` lets a destructive lifecycle op (delete_vault) run
    on an archived vault; every normal mutating caller leaves it False.

    Vault write-policy (P0 S3, design §5.1a): a MARKED vault (a
    `vault_write_policy` row exists) accepts a mutating call
    (writer/admin/owner) ONLY from a PAT on its `vault_write_grants`
    allowlist — this denies even the vault OWNER and a JWT session. The
    one escape hatch is an *unscoped* system admin, which bypasses with
    a loud `vault.write_policy_admin_bypass` audit event (a *scoped*
    admin PAT does not bypass). Consequence: `delete_vault` requests
    required_role="admin", one of the guarded roles, so deleting a
    MARKED vault also requires a grant or the admin bypass — the owner
    alone cannot delete it.
    """
    pool = await get_pool()
    uid = uuid.UUID(user_id)

    async with pool.acquire() as conn:
        vault = await conn.fetchrow("SELECT id, name, owner_id, status, public_access FROM vaults WHERE name = $1", vault_name)
        if not vault:
            raise NotFoundError("Vault", vault_name)

        # Archived vault = READ-ONLY for EVERYONE incl. admin/owner. This
        # guard sits BEFORE the is_admin / owner short-circuits below, so
        # even a system admin or the owner is refused a mutation. It fires
        # for any mutating-role request: 'writer' (put/update/table row
        # writes) and 'admin' (drop_table / create_table / alter_table).
        # PG ACL has no archive concept and we intentionally keep the
        # write grants intact (so unarchive is instant) — the block lives
        # here in the app layer. Owner-level lifecycle (unarchive,
        # transfer_ownership) routes through required_role='owner', and
        # delete_vault passes allow_archived=True (you archive then delete).
        if (
            not allow_archived
            and vault["status"] == "archived"
            and required_role in ("writer", "admin")
        ):
            raise ForbiddenError(f"Vault '{vault_name}' is archived (read-only)")

        # External-git mirror vaults are read-only to every user (incl. owner).
        # Mutations come exclusively from the external_git_poller worker,
        # which goes through service-level helpers and bypasses this check.
        if required_role == "writer":
            is_mirror = await conn.fetchval(
                "SELECT 1 FROM vault_external_git WHERE vault_id = $1",
                vault["id"],
            )
            if is_mirror:
                raise ForbiddenError(
                    f"Vault '{vault_name}' is a read-only external git mirror"
                )

        # Option B — per-PAT vault scope (token-scoping backstop). If this
        # request's token carries a vault scope, a MUTATING access
        # (writer/admin/owner) to a vault OUTSIDE the scope is refused here —
        # BEFORE the is_admin / owner short-circuits below, so even a scoped
        # admin token cannot escape its scope. Reads are unaffected (a scoped
        # agent still reads broadly; the scope bounds WRITES only). Effective
        # write permission = user-ACL ∩ scope, for the whole surface that
        # routes through check_vault_access (REST + MCP). NULL token scope ⇒
        # current_vault_scope is None ⇒ historical full-ACL behaviour.
        # The predicate itself lives in `check_vault_scope` — vault
        # CREATION has no row to resolve and calls that helper directly,
        # so both surfaces refuse with one message (dnotitia/akb#284).
        check_vault_scope(vault_name, required_role)

        # Vault write-policy guard (P0 S3, A2/A3 — see the docstring
        # above). Sits BEFORE the is_admin / owner short-circuits below,
        # same placement as the Option B scope guard above, so a MARKED
        # vault denies every caller class unless granted — fail-CLOSED
        # on absence, the opposite of the scope guard's "NULL scope ⇒
        # unrestricted" fail-OPEN idiom above. Only fires for the same
        # mutating roles as that guard.
        if required_role in _MUTATING_ROLES:
            policy = await write_policy_repo.get_policy(vault["id"], conn=conn)
            if policy is not None:
                token_id = current_token_uuid()
                grant_actions = (
                    await write_policy_repo.get_grant_actions(
                        vault["id"], token_id, conn=conn,
                    )
                    if token_id is not None else None
                )
                if grant_actions is not None and (
                    WRITE_ACTION_WILDCARD in grant_actions
                    or (write_action is not None and write_action in grant_actions)
                ):
                    return {
                        "vault_id": vault["id"],
                        "role": required_role,
                        "status": vault["status"],
                        "role_source": "write_policy_grant",
                        "write_grant_actions": sorted(grant_actions),
                    }
                if await _is_unscoped_system_admin(uid, conn):
                    # A3: bypass, but LOUDLY — never a silent escape hatch.
                    await emit_event(
                        conn, "vault.write_policy_admin_bypass",
                        vault_id=vault["id"],
                        actor_id=str(uid),
                        payload={
                            "managed_by": policy["managed_by"],
                            "required_role": required_role,
                            "vault": vault_name,
                        },
                    )
                    return {
                        "vault_id": vault["id"],
                        "role": "owner",
                        "status": vault["status"],
                        "role_source": "write_policy_admin_bypass",
                    }
                raise ForbiddenError(
                    f"Vault '{vault_name}' is write-managed by "
                    f"{policy['managed_by']}; writes require a granted "
                    f"service token with the required write action"
                )

        # System admin bypasses all vault ACL. `role` stays "owner" so every
        # required_role gate keeps passing, but `role_source` must tell the
        # truth: labelling the bypass "member" made downstream callers
        # (get_vault_info's role field, transfer_ownership's owner re-check)
        # believe the admin literally owns the vault.
        is_admin = await conn.fetchval("SELECT is_admin FROM users WHERE id = $1", uid)
        if is_admin:
            return {
                "vault_id": vault["id"],
                "role": "owner",
                "status": vault["status"],
                "role_source": "system_admin",
            }

        # Owner always has full access
        if vault["owner_id"] == uid:
            return {"vault_id": vault["id"], "role": "owner", "status": vault["status"], "role_source": "member"}

        # Public vault access (none / reader / writer)
        public_access = vault.get("public_access", "none")
        if public_access != "none" and _role_level(required_role) <= _role_level(public_access):
            return {"vault_id": vault["id"], "role": public_access, "status": vault["status"], "role_source": "public"}

        # Check vault_access table
        access = await conn.fetchrow(
            "SELECT role FROM vault_access WHERE vault_id = $1 AND user_id = $2",
            vault["id"], uid,
        )

        user_role = access["role"] if access else None
        if not user_role or _role_level(user_role) < _role_level(required_role):
            raise ForbiddenError(f"Requires '{required_role}' role on vault '{vault_name}'")

        return {"vault_id": vault["id"], "role": user_role, "status": vault["status"], "role_source": "member"}


async def check_delegated_vault_writer(user_id: str, vault_name: str) -> dict:
    """Authorize the human half of a dual-principal managed file upload.

    This check deliberately ignores ``vault_write_policy`` because the primary
    action-limited service token already satisfies that boundary. It does not
    inherit public access: a delegated writer must be an active AKB account
    explicitly bound to this Vault (or the owner/system admin).
    """
    pool = await get_pool()
    uid = uuid.UUID(user_id)
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT is_admin, account_status, account_kind FROM users WHERE id = $1",
            uid,
        )
        if not user or user["account_status"] != "active":
            raise ForbiddenError("Delegated AKB account is not active")
        if user["account_kind"] != "human":
            raise ForbiddenError("Delegated AKB account must be a human account")

        vault = await conn.fetchrow(
            "SELECT id, owner_id, status FROM vaults WHERE name = $1", vault_name,
        )
        if not vault:
            raise NotFoundError("Vault", vault_name)
        if vault["status"] == "archived":
            raise ForbiddenError(f"Vault '{vault_name}' is archived (read-only)")
        is_mirror = await conn.fetchval(
            "SELECT 1 FROM vault_external_git WHERE vault_id = $1", vault["id"],
        )
        if is_mirror:
            raise ForbiddenError(f"Vault '{vault_name}' is a read-only external git mirror")
        if user["is_admin"] or vault["owner_id"] == uid:
            return {
                "vault_id": vault["id"],
                "role": "owner",
                "status": vault["status"],
                "role_source": "member",
            }

        access = await conn.fetchrow(
            "SELECT role FROM vault_access WHERE vault_id = $1 AND user_id = $2",
            vault["id"], uid,
        )
        role = access["role"] if access else None
        if role is None or _role_level(role) < _role_level("writer"):
            raise ForbiddenError(
                f"Delegated account requires explicit 'writer' role on vault '{vault_name}'"
            )
        return {
            "vault_id": vault["id"],
            "role": role,
            "status": vault["status"],
            "role_source": "member",
        }


async def get_user_role(user_id: str, vault_name: str) -> str | None:
    """Get user's role on a vault, or None if no access."""
    pool = await get_pool()
    uid = uuid.UUID(user_id)

    async with pool.acquire() as conn:
        vault = await conn.fetchrow("SELECT id, owner_id FROM vaults WHERE name = $1", vault_name)
        if not vault:
            return None
        if vault["owner_id"] == uid:
            return "owner"

        access = await conn.fetchrow(
            "SELECT role FROM vault_access WHERE vault_id = $1 AND user_id = $2",
            vault["id"], uid,
        )
        return access["role"] if access else None


# ── Grant / Revoke ───────────────────────────────────────────

async def grant_access(
    granter_id: str, vault_name: str, target_username: str, role: str,
) -> dict:
    """Grant vault access to a user. Granter must be owner or admin."""
    if role not in VALID_ROLES or role == "owner":
        raise ForbiddenError(f"Invalid role: {role}. Use: reader, writer, admin")

    # Verify granter has permission BEFORE the mutation transaction. The
    # actual mutation re-acquires a FOR UPDATE row lock on the vault to
    # close the TOCTOU window where a concurrent revoke removes the
    # granter's admin role between this check and the INSERT (06-F2).
    await check_vault_access(granter_id, vault_name, required_role="admin")

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            vault = await conn.fetchrow(
                "SELECT id, owner_id, status FROM vaults WHERE name = $1 FOR UPDATE",
                vault_name,
            )
            if not vault:
                raise NotFoundError("Vault", vault_name)
            if vault["status"] == "archived":
                raise ForbiddenError(f"Vault '{vault_name}' is archived (read-only)")
            # Re-verify granter's role under the row lock (TOCTOU close).
            granter_uid = uuid.UUID(granter_id)
            if vault["owner_id"] != granter_uid:
                is_admin = await conn.fetchval(
                    "SELECT is_admin FROM users WHERE id = $1", granter_uid,
                )
                if not is_admin:
                    granter_role = await conn.fetchval(
                        "SELECT role FROM vault_access WHERE vault_id = $1 AND user_id = $2",
                        vault["id"], granter_uid,
                    )
                    if _role_level(granter_role or "") < _role_level("admin"):
                        raise ForbiddenError(
                            f"Requires 'admin' role on vault '{vault_name}'"
                        )
            target = await conn.fetchrow(
                "SELECT id, username FROM users WHERE username = $1", target_username,
            )
            if not target:
                raise NotFoundError("User", target_username)
            await conn.execute(
                """
                INSERT INTO vault_access (id, vault_id, user_id, role, granted_by)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (vault_id, user_id)
                DO UPDATE SET role = $4, granted_by = $5
                """,
                uuid.uuid4(), vault["id"], target["id"], role, granter_uid,
            )
            await emit_event(
                conn, "access.grant",
                vault_id=vault["id"],
                resource_uri=vault_uri(vault_name),
                actor_id=str(granter_uid),
                payload={"vault": vault_name, "user": target_username, "role": role},
            )

    # PG-native RBAC: GRANT akb_vault_<vid>_<role> TO akb_user_<uid>.
    # Best-effort — reconciler covers drift.
    await get_role_sync().on_grant(vault["id"], target["id"], role)

    logger.info("Granted %s role to %s on vault %s", role, target_username, vault_name)
    return {"vault": vault_name, "user": target_username, "role": role, "granted": True}


async def revoke_access(revoker_id: str, vault_name: str, target_username: str) -> dict:
    """Revoke vault access from a user. Revoker must be owner or admin."""
    await check_vault_access(revoker_id, vault_name, required_role="admin")

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            vault = await conn.fetchrow(
                "SELECT id, owner_id, status FROM vaults WHERE name = $1 FOR UPDATE",
                vault_name,
            )
            if not vault:
                raise NotFoundError("Vault", vault_name)
            if vault["status"] == "archived":
                raise ForbiddenError(f"Vault '{vault_name}' is archived (read-only)")
            revoker_uid = uuid.UUID(revoker_id)
            if vault["owner_id"] != revoker_uid:
                is_admin = await conn.fetchval(
                    "SELECT is_admin FROM users WHERE id = $1", revoker_uid,
                )
                if not is_admin:
                    revoker_role = await conn.fetchval(
                        "SELECT role FROM vault_access WHERE vault_id = $1 AND user_id = $2",
                        vault["id"], revoker_uid,
                    )
                    if _role_level(revoker_role or "") < _role_level("admin"):
                        raise ForbiddenError(
                            f"Requires 'admin' role on vault '{vault_name}'"
                        )
            target = await conn.fetchrow(
                "SELECT id FROM users WHERE username = $1", target_username,
            )
            if not target:
                raise NotFoundError("User", target_username)
            if vault["owner_id"] == target["id"]:
                raise ForbiddenError(
                    "Cannot revoke owner's access. Use transfer_ownership instead."
                )
            await conn.execute(
                "DELETE FROM vault_access WHERE vault_id = $1 AND user_id = $2",
                vault["id"], target["id"],
            )
            await emit_event(
                conn, "access.revoke",
                vault_id=vault["id"],
                resource_uri=vault_uri(vault_name),
                actor_id=str(revoker_uid),
                payload={"vault": vault_name, "user": target_username},
            )

    # PG-native RBAC: REVOKE all vault group memberships from akb_user_<uid>.
    await get_role_sync().on_revoke(vault["id"], target["id"])

    logger.info("Revoked access for %s on vault %s", target_username, vault_name)
    return {"vault": vault_name, "user": target_username, "revoked": True}


# ── Vault members ────────────────────────────────────────────

async def list_vault_members(user_id: str, vault_name: str) -> list[dict]:
    """List all members of a vault. Requires at least reader access."""
    await check_vault_access(user_id, vault_name, required_role="reader")

    pool = await get_pool()
    async with pool.acquire() as conn:
        vault = await conn.fetchrow("SELECT id, owner_id FROM vaults WHERE name = $1", vault_name)

        # Get owner
        owner = await conn.fetchrow("SELECT username, display_name, email FROM users WHERE id = $1", vault["owner_id"])
        members = []
        if owner:
            members.append({
                "username": owner["username"],
                "display_name": owner["display_name"],
                "email": owner["email"],
                "role": "owner",
            })

        # Get other members
        rows = await conn.fetch(
            """
            SELECT u.username, u.display_name, u.email, va.role, va.created_at
            FROM vault_access va
            JOIN users u ON va.user_id = u.id
            WHERE va.vault_id = $1
            ORDER BY va.role, u.username
            """,
            vault["id"],
        )
        for r in rows:
            members.append({
                "username": r["username"],
                "display_name": r["display_name"],
                "email": r["email"],
                "role": r["role"],
                "since": r["created_at"].isoformat() if r["created_at"] else None,
            })

    return members


# ── User-accessible vaults ──────────────────────────────────

async def list_accessible_vaults(user_id: str) -> list[dict]:
    """List all vaults the user has access to, with their role."""
    pool = await get_pool()
    uid = uuid.UUID(user_id)

    async with pool.acquire() as conn:
        # System admin sees all vaults
        is_admin = await conn.fetchval("SELECT is_admin FROM users WHERE id = $1", uid)

        # P0 S3 (design §5.1a): explicit LEFT JOIN on the 1:1
        # vault_write_policy sidecar in both branches — a vault has at
        # most one policy row (vault_id is its PK) so this never fans out
        # rows. NULL (no match) reads as ungoverned, same convention as
        # `get_vault_info`.
        if is_admin:
            rows = await conn.fetch(
                """
                SELECT v.id, v.name, v.description, v.status, v.created_at,
                       COALESCE(CASE WHEN v.owner_id = $1 THEN 'owner' END, 'admin') as role,
                       vwp.managed_by
                FROM vaults v
                LEFT JOIN vault_write_policy vwp ON v.id = vwp.vault_id
                ORDER BY v.name
                """,
                uid,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT v.id, v.name, v.description, v.status, v.created_at,
                       COALESCE(va.role, CASE WHEN v.owner_id = $1 THEN 'owner' WHEN v.public_access != 'none' THEN v.public_access END) as role,
                       vwp.managed_by
                FROM vaults v
                LEFT JOIN vault_access va ON v.id = va.vault_id AND va.user_id = $1
                LEFT JOIN vault_write_policy vwp ON v.id = vwp.vault_id
                WHERE v.owner_id = $1 OR va.user_id = $1 OR v.public_access != 'none'
                ORDER BY v.name
                """,
                uid,
            )

        return [
            {
                "id": str(r["id"]),
                "name": r["name"],
                "description": r["description"],
                "status": r["status"],
                "role": r["role"],
                "managed_by": r["managed_by"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]


# ── Vault info ───────────────────────────────────────────────

async def get_vault_info(user_id: str, vault_name: str) -> dict:
    """Get detailed vault info. Requires reader access. Includes the caller's
    effective role and the lifecycle/public-access/external-mirror flags the
    UI uses to gate owner-only controls and render state badges."""
    access = await check_vault_access(user_id, vault_name, required_role="reader")
    caller_role = access["role"]
    role_source = access["role_source"]

    pool = await get_pool()
    # Fan out the eight independent counts/lookups onto the connection pool
    # in parallel — they don't depend on each other and used to run
    # sequentially on a single connection (~8 round-trips of latency stacked
    # on every page load).
    async def _q(query: str, *args):
        async with pool.acquire() as c:
            return await c.fetchval(query, *args)

    async def _r(query: str, *args):
        async with pool.acquire() as c:
            return await c.fetchrow(query, *args)

    vault = await _r("SELECT * FROM vaults WHERE name = $1", vault_name)
    vid = vault["id"]
    if (
        role_source in ("system_admin", "write_policy_admin_bypass")
        and vault["owner_id"] != uuid.UUID(user_id)
    ):
        # An admin bypass is access, not ownership. Report the same "admin"
        # label `list_accessible_vaults` uses for this caller/vault pair so
        # consumers gating on role == "owner" (ownership transfer UIs, the
        # pipeline's Pattern 35 target-vault gate) cannot mistake a
        # superuser grant for ownership.
        caller_role = "admin"
    (
        owner,
        member_count,
        doc_count,
        table_count,
        file_count,
        coll_count,
        edge_count,
        last_doc,
        is_external_git,
        write_policy,
    ) = await asyncio.gather(
        _r("SELECT username, display_name FROM users WHERE id = $1", vault["owner_id"]),
        _q("SELECT COUNT(*) FROM vault_access WHERE vault_id = $1", vid),
        _q("SELECT COUNT(*) FROM documents WHERE vault_id = $1", vid),
        _q("SELECT COUNT(*) FROM vault_tables WHERE vault_id = $1", vid),
        _q(
            "SELECT COUNT(*) FROM vault_files vf WHERE vault_id = $1 AND "
            + confirmed_file_predicate("vf"),
            vid,
        ),
        # Authoritative collection total — depth-safe, unlike a client-side
        # browse(depth=2) count which silently undercounts deeper nesting.
        _q("SELECT COUNT(*) FROM collections WHERE vault_id = $1", vid),
        _q("SELECT COUNT(*) FROM edges WHERE vault_id = $1", vid),
        _r(
            "SELECT updated_at, created_by FROM documents WHERE vault_id = $1 "
            "ORDER BY updated_at DESC LIMIT 1",
            vid,
        ),
        _q("SELECT 1 FROM vault_external_git WHERE vault_id = $1", vid),
        # P0 S3: vault_write_policy is a 1:1 sidecar keyed on vault_id —
        # reuse the repo (own pool-acquire, same fan-out style as the
        # helpers above) rather than a bespoke query here.
        write_policy_repo.get_policy(vid),
    )

    tables = await _list_tables_with_schema(vault_name, vid) if table_count else []

    return {
        "name": vault["name"],
        "description": vault["description"],
        "status": vault["status"],
        "is_archived": vault["status"] == "archived",
        # P0 S3 (design §5.1a): None when ungoverned; otherwise the
        # write-policy owner label (e.g. "collector:acme-jira",
        # "gardener:distill") — naut's `normalizeAkbVaultList` and any
        # other consumer key routing decisions off this one field.
        "managed_by": write_policy["managed_by"] if write_policy else None,
        "is_external_git": bool(is_external_git),
        "public_access": vault["public_access"],
        "role": caller_role,
        "role_source": role_source,
        "owner": owner["username"] if owner else None,
        "owner_display_name": owner["display_name"] if owner else None,
        "member_count": member_count + 1,  # +1 for owner
        "collection_count": coll_count,
        "document_count": doc_count,
        "table_count": table_count,
        "file_count": file_count,
        "edge_count": edge_count,
        "tables": tables,
        "last_activity": last_doc["updated_at"].isoformat() if last_doc else None,
        "last_active_user": last_doc["created_by"] if last_doc else None,
        "created_at": vault["created_at"].isoformat(),
    }


async def _list_tables_with_schema(vault_name: str, vault_id) -> list[dict]:
    """Return [{name, row_count, columns: [{name, type, example?}]}, …]
    for every table in `vault_id`.

    Pre-loads schema + sample so agents don't have to run mid-flow
    `information_schema.columns` lookups (issue #34 KISA RAG PoC pattern —
    122 such calls observed across 107 queries).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        registry = await conn.fetch(
            "SELECT id, name, unique_keys, indexes FROM vault_tables "
            "WHERE vault_id = $1 ORDER BY name",
            vault_id,
        )
        if not registry:
            return []

        # All columns for the vault's vt_* tables in one query — use the
        # canonical sanitizer so hyphenated vault names map to the actual
        # `vt_<sanitised>__<sanitised>` PG identifiers.
        from app.repositories.table_data_repo import pg_table_name
        from app.repositories import table_registry_repo
        pg_names = [pg_table_name(vault_name, r["name"]) for r in registry]
        col_rows = await conn.fetch(
            """
            SELECT c.relname AS table_name, a.attname AS name,
                   format_type(a.atttypid, a.atttypmod) AS type, a.attnum
              FROM pg_attribute a
              JOIN pg_class c ON c.oid = a.attrelid
             WHERE c.relname = ANY($1::text[])
               AND a.attnum > 0
               AND NOT a.attisdropped
             ORDER BY c.relname, a.attnum
            """,
            pg_names,
        )
        by_table: dict[str, list[dict]] = {}
        for row in col_rows:
            col: dict = {"name": row["name"], "type": row["type"]}
            if row["type"] == "jsonb":
                col["search_hint"] = f"{row['name']}::text ILIKE '%X%'"
            by_table.setdefault(row["table_name"], []).append(col)

        # Row counts + one-row sample (only when row_count > 0).
        out: list[dict] = []
        for r in registry:
            pg_name = pg_table_name(vault_name, r["name"])
            # Identifier is built from validated vault + table names —
            # vault_tables.name is constrained by `akb_create_table`
            # validation, so direct interpolation is safe.
            row_count = await conn.fetchval(f'SELECT COUNT(*) FROM "{pg_name}"')
            columns = by_table.get(pg_name, [])
            if row_count and columns:
                sample = await conn.fetchrow(f'SELECT * FROM "{pg_name}" LIMIT 1')
                example_map = dict(sample) if sample else {}
                for col in columns:
                    val = example_map.get(col["name"])
                    if val is not None:
                        col["example"] = _coerce_example(val)
            out.append({
                "name": r["name"],
                "row_count": row_count,
                "columns": columns,
                # Declared guarantees (AKB #215) so an agent inspecting a
                # vault sees uniqueness/index metadata without a mid-flow
                # information_schema lookup — the point of this surface.
                "unique_keys": table_registry_repo.parse_json_list(r["unique_keys"]),
                "indexes": table_registry_repo.parse_json_list(r["indexes"]),
            })
        return out


def _coerce_example(v):
    """JSON-safe coercion for sample values (UUIDs, dates, jsonb, …)."""
    if isinstance(v, uuid.UUID):
        return str(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, (int, float, str, bool, list, dict)):
        return v
    return str(v)


# ── Transfer ownership ──────────────────────────────────────

async def transfer_ownership(owner_id: str, vault_name: str, new_owner_username: str) -> dict:
    """Transfer vault ownership. The current owner or a system admin can do this.

    All three mutations (owner update, old-owner-to-admin grant, new-owner
    vault_access cleanup) run in ONE transaction so a crash mid-transfer
    cannot leave the vault with no owner / two owners / an admin gap
    (06-F1). The vault row is selected ``FOR UPDATE`` so a concurrent
    transfer also serializes — only one of N parallel transfers wins.
    """
    access = await check_vault_access(owner_id, vault_name, required_role="owner")
    # An admin passes the gate above without literally owning the vault, so
    # the owner-staleness re-check below must not treat that as a lost race
    # (it used to raise a misleading "owner_id moved" conflict on EVERY
    # admin-initiated transfer).
    caller_is_system_admin = access["role_source"] in (
        "system_admin",
        "write_policy_admin_bypass",
    )

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            vault = await conn.fetchrow(
                "SELECT id, owner_id FROM vaults WHERE name = $1 FOR UPDATE",
                vault_name,
            )
            if not vault:
                raise NotFoundError("Vault", vault_name)
            current_owner_uid = uuid.UUID(owner_id)
            # Re-verify ownership under the row lock — a concurrent
            # transfer might have already moved owner_id away from us. An
            # admin's right to transfer does not depend on who currently
            # owns the row (the FOR UPDATE lock still serializes), so the
            # staleness check only applies to owner-initiated transfers.
            if vault["owner_id"] != current_owner_uid and not caller_is_system_admin:
                raise ConflictError(
                    "owner_id moved during transfer (another transfer won the race)"
                )
            new_owner = await conn.fetchrow(
                "SELECT id, username FROM users WHERE username = $1",
                new_owner_username,
            )
            if not new_owner:
                raise NotFoundError("User", new_owner_username)

            await conn.execute(
                "UPDATE vaults SET owner_id = $1 WHERE id = $2",
                new_owner["id"], vault["id"],
            )
            await conn.execute(
                """
                INSERT INTO vault_access (id, vault_id, user_id, role, granted_by)
                VALUES ($1, $2, $3, 'admin', $4)
                ON CONFLICT (vault_id, user_id) DO UPDATE SET role = 'admin'
                """,
                uuid.uuid4(), vault["id"], vault["owner_id"], new_owner["id"],
            )
            await conn.execute(
                "DELETE FROM vault_access WHERE vault_id = $1 AND user_id = $2",
                vault["id"], new_owner["id"],
            )
            await emit_event(
                conn, "access.transfer_ownership",
                vault_id=vault["id"],
                resource_uri=vault_uri(vault_name),
                actor_id=str(current_owner_uid),
                payload={
                    "vault": vault_name,
                    "from_user_id": str(vault["owner_id"]),
                    "to_username": new_owner_username,
                },
            )

    # PG-native RBAC: mirror the two membership outcomes —
    #   - new owner gets admin (vaults.owner_id moved)
    #   - old owner gets admin (vault_access row added above)
    # `on_grant("admin")` is idempotent and internally clears any
    # weaker (reader/writer) membership the user previously had,
    # so no explicit on_revoke step is required here.
    rs = get_role_sync()
    await rs.on_grant(vault["id"], new_owner["id"], "admin")
    await rs.on_grant(vault["id"], vault["owner_id"], "admin")

    logger.info("Transferred ownership of %s to %s", vault_name, new_owner_username)
    return {"vault": vault_name, "new_owner": new_owner_username, "transferred": True}


# ── User search ──────────────────────────────────────────────

async def search_users(query: str | None = None, limit: int = 20) -> list[dict]:
    """Search users by username or display_name."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if query:
            rows = await conn.fetch(
                """
                SELECT username, display_name, email
                FROM users
                WHERE username ILIKE $1 OR display_name ILIKE $1 OR email ILIKE $1
                ORDER BY username
                LIMIT $2
                """,
                f"%{query}%", limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT username, display_name, email FROM users ORDER BY username LIMIT $1",
                limit,
            )

    return [
        {"username": r["username"], "display_name": r["display_name"], "email": r["email"]}
        for r in rows
    ]


async def list_all_users_admin() -> list[dict]:
    """Admin-only: list every user with vault counts. Caller must gate on is_admin."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT u.id, u.username, u.display_name, u.email, u.is_admin,
                   u.auth_provider, u.account_status, u.account_kind, u.created_at,
                   (SELECT COUNT(*) FROM vaults v WHERE v.owner_id = u.id) AS owned_vaults
            FROM users u
            ORDER BY u.created_at
            """
        )
    return [
        {
            "id": str(r["id"]),
            "username": r["username"],
            "display_name": r["display_name"],
            "email": r["email"],
            "is_admin": r["is_admin"],
            "auth_provider": r["auth_provider"],
            "account_status": r["account_status"],
            "account_kind": r["account_kind"],
            "created_at": r["created_at"].isoformat(),
            "owned_vaults": r["owned_vaults"],
        }
        for r in rows
    ]


# ── Archive vault ────────────────────────────────────────────

async def archive_vault(user_id: str, vault_name: str) -> dict:
    """Archive a vault (read-only). Only owner can do this."""
    await check_vault_access(user_id, vault_name, required_role="owner")

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE vaults SET status = 'archived', updated_at = NOW() WHERE name = $1",
            vault_name,
        )

    logger.info("Archived vault: %s", vault_name)
    return {"vault": vault_name, "status": "archived"}


async def unarchive_vault(user_id: str, vault_name: str) -> dict:
    """Restore an archived vault to active. Only owner can do this.
    `check_vault_access` skips its archived-check for non-writer roles, so
    asking for owner-level access here works on archived rows too."""
    await check_vault_access(user_id, vault_name, required_role="owner")

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE vaults SET status = 'active', updated_at = NOW() WHERE name = $1",
            vault_name,
        )

    logger.info("Unarchived vault: %s", vault_name)
    return {"vault": vault_name, "status": "active"}


async def update_vault_metadata(
    user_id: str,
    vault_name: str,
    description: str | None = None,
    public_access: str | None = None,
) -> dict:
    """Update vault metadata (description, public_access). Owner-only.

    Either field may be omitted to leave it untouched. Public access goes
    through the same enum guard as create_vault so a typo can't slip in
    via PATCH that wouldn't have been allowed at create time."""
    await check_vault_access(user_id, vault_name, required_role="owner")

    sets: list[str] = []
    args: list = []
    if description is not None:
        sets.append(f"description = ${len(args) + 1}")
        args.append(description)
    if public_access is not None:
        validate_public_access(public_access)
        sets.append(f"public_access = ${len(args) + 1}")
        args.append(public_access)
    if not sets:
        return {"vault": vault_name, "updated": False}

    args.append(vault_name)
    sql = f"UPDATE vaults SET {', '.join(sets)}, updated_at = NOW() WHERE name = ${len(args)}"

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(sql, *args)
        if public_access is not None:
            # Need vault_id to update PG ACL via RoleSync.
            vault_id = await conn.fetchval(
                "SELECT id FROM vaults WHERE name = $1", vault_name,
            )
            if vault_id is not None:
                await get_role_sync().on_public_access_change(vault_id, public_access)

    logger.info("Updated vault metadata: %s", vault_name)
    return {"vault": vault_name, "updated": True}


async def set_public_access(user_id: str, vault_name: str, level: str) -> dict:
    """Owner-only mutation of `vaults.public_access`.

    Two writes happen atomically from the caller's perspective:
      1. UPDATE vaults SET public_access = $level
      2. RoleSync.on_public_access_change → grant/revoke
         akb_vault_<vid>_<scope> TO akb_authenticated so any
         authenticated user can read/write the vault via akb_sql.

    Centralised here so `akb_set_public` (MCP) and any future REST
    endpoint share the lifecycle plumbing without duplicating the SQL
    or the RBAC hook call.
    """
    level = validate_public_access(level)
    await check_vault_access(user_id, vault_name, required_role="owner")

    pool = await get_pool()
    async with pool.acquire() as conn:
        vault = await conn.fetchrow(
            "SELECT id, status FROM vaults WHERE name = $1", vault_name,
        )
        if not vault:
            raise NotFoundError("Vault", vault_name)
        if vault["status"] == "archived":
            raise ForbiddenError(f"Vault '{vault_name}' is archived (read-only)")
        await conn.execute(
            "UPDATE vaults SET public_access = $1, updated_at = NOW() WHERE id = $2",
            level, vault["id"],
        )
        await emit_event(
            conn, "vault.public_access",
            vault_id=vault["id"],
            resource_uri=vault_uri(vault_name),
            actor_id=user_id,
            payload={"vault": vault_name, "level": level},
        )

    # PG-native RBAC: grant/revoke the corresponding vault group role
    # TO akb_authenticated so public access maps to a real PG ACL.
    await get_role_sync().on_public_access_change(vault["id"], level)

    logger.info("Set public_access for %s → %s", vault_name, level)
    return {"vault": vault_name, "public_access": level}


# ── Vault write-policy admin (P0 S3, design §5.1a) ──────────
#
# System-admin-only surface (enforced at the route layer via
# `_require_admin` in `api/routes/access.py` — same convention every other
# `/admin/...` function in this module already uses, e.g.
# `account_service.suspend_user`: this module trusts the caller and takes
# `actor_id` purely for the audit trail, it never re-checks `users.is_admin`
# itself). Every mutation here emits `vault.write_policy_changed`
# UNCONDITIONALLY — even when the call is a no-op against current state
# (re-marking with the same managed_by, unmarking an already-unmarked
# vault, removing a grant that was never added) — because the point of the
# audit trail is "an admin invoked this action", not "state changed since
# last read". That is also what makes the unmark → write → re-mark
# break-glass sequence (Task 8/9's handoff note) fully auditable: a smart
# no-op dedup would hide exactly the events that sequence needs recorded.

async def set_vault_write_policy(
    actor_id: str, vault_name: str, managed_by: str, note: str | None = None,
) -> dict:
    """Admin-only: mark ``vault_name`` write-managed.

    Once marked, `check_vault_access`'s write-policy guard rejects every
    mutating call (including the vault OWNER and any JWT session) unless
    the caller's PAT is on the vault's `vault_write_grants` allowlist. Idempotent
    upsert: re-marking an already-marked vault REPLACES managed_by and note
    (note omitted ⇒ cleared); grants are preserved (`vault_write_policy_repo.set_policy`
    pins `created_at`/`created_by` to the original mark).

    External-git mirror vaults are REJECTED (409), not silently accepted:
    see the comment at the mirror check below for the reasoning.
    """
    managed_by = managed_by.strip()
    if not managed_by:
        raise ValidationError("managed_by must not be empty")

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            vault = await conn.fetchrow(
                "SELECT id FROM vaults WHERE name = $1 FOR UPDATE", vault_name,
            )
            if not vault:
                raise NotFoundError("Vault", vault_name)

            # DECISION (task 10): an external-git mirror vault is ALREADY
            # read-only to every caller via its own unconditional guard in
            # `check_vault_access` (writer-role mirror check, above the
            # write-policy guard in that same function) — mutations there
            # come exclusively from the in-process poller, which bypasses
            # `check_vault_access` entirely. A grant on such a vault could
            # therefore never make a PAT write succeed: the mirror check
            # fires first and unconditionally, regardless of any
            # `vault_write_grants` row. Marking it anyway would be
            # harmless in enforcement terms but actively misleading in
            # practice — an operator reading `managed_by="collector:x"`
            # would reasonably (and wrongly) conclude a granted token can
            # write there. Reject with a clear message rather than ship a
            # marking that can never do what it appears to promise.
            is_mirror = await conn.fetchval(
                "SELECT 1 FROM vault_external_git WHERE vault_id = $1", vault["id"],
            )
            if is_mirror:
                raise ConflictError(
                    f"Vault '{vault_name}' is an external-git mirror "
                    f"(already read-only via its own poller); marking it "
                    f"with a write policy would not change enforcement and "
                    f"misleadingly implies a granted token could write here"
                )

            policy = await write_policy_repo.set_policy(
                vault["id"], managed_by, created_by=actor_id, note=note, conn=conn,
            )
            await emit_event(
                conn, "vault.write_policy_changed",
                vault_id=vault["id"],
                resource_uri=vault_uri(vault_name),
                actor_id=actor_id,
                payload={
                    "action": "marked",
                    "vault": vault_name,
                    "managed_by": managed_by,
                    "note": note,
                },
            )

    logger.info("Marked vault %s write-managed by %s", vault_name, managed_by)
    return {
        "vault": vault_name,
        "managed_by": policy["managed_by"],
        "note": policy["note"],
        "marked": True,
    }


async def bootstrap_vault_write_policy(
    actor_id: str,
    vault_name: str,
    managed_by: str,
    grants: list[dict[str, object]],
    note: str | None = None,
) -> dict:
    """Atomically mark a Vault and install its complete initial writer set.

    The ordinary set-policy and add-grant endpoints remain useful for updates,
    but cannot safely perform an initial cutover: committing the policy first
    creates a visible fail-closed interval before the grants arrive. This path
    validates every token, writes the policy and all grants, and emits its audit
    event in one PostgreSQL transaction. Any failure leaves the Vault unmarked.
    Re-running it is idempotent for the supplied grants and preserves unrelated
    grants already present on an existing policy.
    """
    managed_by = managed_by.strip()
    if not managed_by:
        raise ValidationError("managed_by must not be empty")
    if not grants:
        raise ValidationError("bootstrap grants must not be empty")

    normalized_grants: list[tuple[uuid.UUID, str, tuple[str, ...]]] = []
    seen_token_ids: set[uuid.UUID] = set()
    for grant in grants:
        token_id = grant.get("token_id")
        try:
            tid = uuid.UUID(str(token_id))
        except (AttributeError, TypeError, ValueError):
            raise ValidationError(f"Invalid token_id: {token_id!r}") from None
        if tid in seen_token_ids:
            raise ValidationError(f"Duplicate bootstrap token_id: {tid}")
        seen_token_ids.add(tid)
        write_actions = grant.get("write_actions")
        if write_actions is not None and not isinstance(write_actions, (list, tuple)):
            raise ValidationError("write_actions must be a list of strings")
        actions = normalize_write_actions(write_actions)
        normalized_grants.append((tid, str(tid), actions))

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            vault = await conn.fetchrow(
                "SELECT id FROM vaults WHERE name = $1 FOR UPDATE", vault_name,
            )
            if not vault:
                raise NotFoundError("Vault", vault_name)

            is_mirror = await conn.fetchval(
                "SELECT 1 FROM vault_external_git WHERE vault_id = $1", vault["id"],
            )
            if is_mirror:
                raise ConflictError(
                    f"Vault '{vault_name}' is an external-git mirror "
                    f"(already read-only via its own poller); marking it "
                    f"with a write policy would not change enforcement and "
                    f"misleadingly implies a granted token could write here"
                )

            for tid, token_id, actions in normalized_grants:
                token_row = await conn.fetchrow(
                    """
                    SELECT t.id, t.scopes, t.vault_scope, t.key_class,
                           (t.expires_at IS NULL OR t.expires_at > NOW()) AS is_unexpired,
                           u.account_kind, u.account_status, u.is_admin
                      FROM tokens t
                      JOIN users u ON u.id = t.user_id
                     WHERE t.id = $1
                       FOR SHARE OF t, u
                    """,
                    tid,
                )
                if not token_row:
                    raise NotFoundError("Token", token_id)
                if actions != (WRITE_ACTION_WILDCARD,):
                    _validate_action_limited_grant_token(
                        dict(token_row), vault_name,
                    )

            policy = await write_policy_repo.set_policy(
                vault["id"], managed_by, created_by=actor_id, note=note, conn=conn,
            )
            grant_results = []
            for tid, token_id, actions in normalized_grants:
                await write_policy_repo.add_grant(
                    vault["id"],
                    tid,
                    actor_id,
                    conn=conn,
                    write_actions=actions,
                )
                grant_results.append(
                    {"token_id": token_id, "write_actions": list(actions)}
                )

            await emit_event(
                conn,
                "vault.write_policy_changed",
                vault_id=vault["id"],
                resource_uri=vault_uri(vault_name),
                actor_id=actor_id,
                payload={
                    "action": "bootstrapped",
                    "vault": vault_name,
                    "managed_by": managed_by,
                    "note": note,
                    "grants": grant_results,
                },
            )

    logger.info(
        "Atomically marked vault %s write-managed by %s with %d grants",
        vault_name,
        managed_by,
        len(grant_results),
    )
    return {
        "vault": vault_name,
        "managed_by": policy["managed_by"],
        "note": policy["note"],
        "marked": True,
        "grants": grant_results,
    }


async def remove_vault_write_policy(actor_id: str, vault_name: str) -> dict:
    """Admin-only: unmark ``vault_name`` — restore ordinary ACL-gated writes.

    Idempotent: unmarking a vault that isn't currently marked is a
    harmless no-op (`was_marked: False` in the response distinguishes it
    from a real transition, but it still emits the event — see this
    section's module-level comment for why). Cascades away every grant
    row for this vault too (`vault_write_policy_repo.remove_policy`'s
    `ON DELETE CASCADE`).

    ROLLBACK PROOF: this is the operation that restores every caller
    (including a plain JWT session) to full pre-marking behaviour — the
    e2e's final step calls this and then re-attempts an ungranted write to
    prove the guard is a true no-op once unmarked, not a one-way ratchet.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            vault = await conn.fetchrow(
                "SELECT id FROM vaults WHERE name = $1 FOR UPDATE", vault_name,
            )
            if not vault:
                raise NotFoundError("Vault", vault_name)

            existing = await write_policy_repo.get_policy(vault["id"], conn=conn)
            await write_policy_repo.remove_policy(vault["id"], conn=conn)
            await emit_event(
                conn, "vault.write_policy_changed",
                vault_id=vault["id"],
                resource_uri=vault_uri(vault_name),
                actor_id=actor_id,
                payload={
                    "action": "unmarked",
                    "vault": vault_name,
                    "managed_by": existing["managed_by"] if existing else None,
                },
            )

    logger.info("Unmarked vault %s (write-policy removed)", vault_name)
    return {"vault": vault_name, "unmarked": True, "was_marked": existing is not None}


async def add_vault_write_grant(
    actor_id: str,
    vault_name: str,
    token_id: str,
    *,
    write_actions: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """Admin-only: grant ``token_id`` write access to a MARKED vault.

    Requires the vault to already be marked (409 `ConflictError` — a grant
    on an ungoverned vault has no allowlist to join and almost certainly
    means the caller meant to mark first) and `token_id` to exist (404).
    Legacy wildcard grants remain class-agnostic. An explicit action-limited
    grant is a stronger managed capability: it requires an active, unexpired,
    non-admin service account token with exactly coarse ``write`` scope and an
    exact one-Vault scope. This makes configuration errors fail at grant time
    instead of on the first product request.
    """
    actions_were_explicit = write_actions is not None
    normalized_actions = normalize_write_actions(write_actions)
    try:
        tid = uuid.UUID(token_id)
    except (AttributeError, TypeError, ValueError):
        raise ValidationError(f"Invalid token_id: {token_id!r}") from None

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            vault = await conn.fetchrow(
                "SELECT id FROM vaults WHERE name = $1 FOR UPDATE", vault_name,
            )
            if not vault:
                raise NotFoundError("Vault", vault_name)

            policy = await write_policy_repo.get_policy(vault["id"], conn=conn)
            if policy is None:
                raise ConflictError(
                    f"Vault '{vault_name}' is not write-managed; mark it "
                    f"first with PUT .../write-policy"
                )

            token_row = await conn.fetchrow(
                """
                SELECT t.id, t.scopes, t.vault_scope, t.key_class,
                       (t.expires_at IS NULL OR t.expires_at > NOW()) AS is_unexpired,
                       u.account_kind, u.account_status, u.is_admin
                  FROM tokens t
                  JOIN users u ON u.id = t.user_id
                 WHERE t.id = $1
                   FOR SHARE OF t, u
                """,
                tid,
            )
            if not token_row:
                raise NotFoundError("Token", token_id)
            if normalized_actions != (WRITE_ACTION_WILDCARD,):
                _validate_action_limited_grant_token(dict(token_row), vault_name)

            await write_policy_repo.add_grant(
                vault["id"],
                tid,
                actor_id,
                conn=conn,
                write_actions=normalized_actions,
            )
            await emit_event(
                conn, "vault.write_policy_changed",
                vault_id=vault["id"],
                resource_uri=vault_uri(vault_name),
                actor_id=actor_id,
                payload={
                    "action": "grant_added",
                    "vault": vault_name,
                    "managed_by": policy["managed_by"],
                    "token_id": token_id,
                    "write_actions": list(normalized_actions),
                },
            )

    logger.info("Granted token %s write access to vault %s", token_id, vault_name)
    result = {
        "vault": vault_name,
        "token_id": token_id,
        "granted": True,
    }
    if actions_were_explicit:
        result["write_actions"] = list(normalized_actions)
    return result


async def remove_vault_write_grant(actor_id: str, vault_name: str, token_id: str) -> dict:
    """Admin-only: revoke ``token_id``'s write grant on ``vault_name``.

    Idempotent (a token that was never granted is a harmless no-op — same
    "always audit the action" rationale as `remove_vault_write_policy`).
    Does NOT require the vault to still be marked — unlike the grant-add
    direction, there is nothing to protect here: if the vault was
    unmarked, the CASCADE already removed every grant, so this is
    naturally a no-op regardless. `token_id` must still exist (404) — same
    operator-typo guard as the grant-add direction.
    """
    try:
        tid = uuid.UUID(token_id)
    except (AttributeError, TypeError, ValueError):
        raise ValidationError(f"Invalid token_id: {token_id!r}") from None

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            vault = await conn.fetchrow(
                "SELECT id FROM vaults WHERE name = $1 FOR UPDATE", vault_name,
            )
            if not vault:
                raise NotFoundError("Vault", vault_name)

            token_row = await conn.fetchrow("SELECT id FROM tokens WHERE id = $1", tid)
            if not token_row:
                raise NotFoundError("Token", token_id)

            policy = await write_policy_repo.get_policy(vault["id"], conn=conn)
            await write_policy_repo.remove_grant(vault["id"], tid, conn=conn)
            await emit_event(
                conn, "vault.write_policy_changed",
                vault_id=vault["id"],
                resource_uri=vault_uri(vault_name),
                actor_id=actor_id,
                payload={
                    "action": "grant_removed",
                    "vault": vault_name,
                    "managed_by": policy["managed_by"] if policy else None,
                    "token_id": token_id,
                },
            )

    logger.info("Revoked token %s write access to vault %s", token_id, vault_name)
    return {"vault": vault_name, "token_id": token_id, "revoked": True}


# ── Destructive: vault delete ───────────────────────────────


async def delete_vault(user_id: str, vault_name: str) -> dict:
    """Permanently delete a vault and all its data. Owner or admin only.

    Cascades:
      - S3 file objects (via vault_files)
      - edges, chunks (via vector-store outbox + sync vector-store delete in
        index_service.delete_vault_chunks)
      - vault_tables (drops the underlying PG tables)
      - documents, collections, vault_access
      - the vault row itself
      - git bare repo directory

    Extracted from the MCP `akb_delete_vault` handler so the REST
    self-delete endpoint can reuse the same path.
    """
    from app.config import settings
    from app.repositories import table_data_repo
    from app.services.index_service import delete_vault_chunks

    # Deletion is a lifecycle op that must work on archived vaults (you
    # archive, then delete) — bypass the archived read-only guard here.
    await check_vault_access(
        user_id, vault_name, required_role="admin", allow_archived=True,
    )
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Lock before enumerating object keys. Image uploads register a
            # pending row before PUT and revalidate this lock at finalization,
            # so the sweep includes every key that can become readable.
            vault = await conn.fetchrow(
                "SELECT id, git_path FROM vaults WHERE name = $1 FOR UPDATE",
                vault_name,
            )
            if not vault:
                return err(f"Vault not found: {vault_name}", code=NOT_FOUND)
            vault_id = vault["id"]
            is_native_ledger_vault = str(vault["git_path"]).startswith("native-ledger://")

            # Capture every object key while the vault lock makes the set
            # complete with respect to concurrent image uploads. Remote S3 I/O
            # must not run while this transaction and row lock are held: it can
            # be slow, cannot roll back, and used to lose failed deletes after
            # the metadata cascade. Enqueue the immutable keys in the same PG
            # transaction instead; the existing worker retries idempotent
            # object deletion after the vault's access has been revoked.
            file_rows = await conn.fetch(
                "SELECT id, s3_key, upload_state FROM vault_files WHERE vault_id = $1",
                vault_id,
            )
            if file_rows and settings.s3_endpoint_url:
                from app.services.s3_delete_worker import (
                    enqueue_delete,
                    enqueue_pending_upload_delete,
                )
                for fr in file_rows:
                    if fr["upload_state"] == "pending":
                        await enqueue_pending_upload_delete(conn, fr["s3_key"])
                    else:
                        await enqueue_delete(conn, fr["s3_key"])

            # Publication snapshots live outside the vault file prefix.
            snap_rows = await conn.fetch(
                "SELECT snapshot_s3_key FROM publications"
                " WHERE vault_id = $1 AND snapshot_s3_key IS NOT NULL",
                vault_id,
            )
            if snap_rows and settings.s3_endpoint_url:
                from app.services.s3_delete_worker import enqueue_delete
                for sr in snap_rows:
                    await enqueue_delete(conn, sr["snapshot_s3_key"])

            from app.services.index_service import _drop_source_chunks_with_outbox

            # Enqueue file-chunk vector deletes BEFORE deleting vault_files.
            # chunks.vault_id CASCADE removes file chunks from PG when the
            # vaults row drops, but vector_delete_outbox doesn't ride the
            # cascade — so the file ids must be captured (from file_rows,
            # read above) and enqueued while they still exist, regardless
            # of whether the S3 branch already removed the vault_files rows.
            for fr in file_rows:
                await _drop_source_chunks_with_outbox(conn, "file", str(fr["id"]))

            if file_rows and settings.s3_endpoint_url:
                await conn.execute("DELETE FROM vault_files WHERE vault_id = $1", vault_id)

            await conn.execute("DELETE FROM edges WHERE vault_id = $1", vault_id)
            await delete_vault_chunks(conn, vault_id)

            # Drop table metadata chunks BEFORE the registry DELETE so the
            # outbox is enqueued against the still-extant source_id.
            # delete_vault_chunks handles legacy and native documents;
            # tables/files still need explicit cleanup because chunks.source_id
            # has no FK (polymorphic source) and would orphan otherwise.
            vtables = await conn.fetch(
                "SELECT id, name FROM vault_tables WHERE vault_id = $1",
                vault_id,
            )
            for vt in vtables:
                await _drop_source_chunks_with_outbox(
                    conn, "table", str(vt["id"]),
                )
                pg_name = table_data_repo.pg_table_name(vault_name, vt["name"])
                await conn.execute(f"DROP TABLE IF EXISTS {pg_name} CASCADE")
            await conn.execute("DELETE FROM vault_tables WHERE vault_id = $1", vault_id)

            await conn.execute("DELETE FROM documents WHERE vault_id = $1", vault_id)
            await conn.execute("DELETE FROM collections WHERE vault_id = $1", vault_id)
            await conn.execute("DELETE FROM vault_access WHERE vault_id = $1", vault_id)
            await conn.execute("DELETE FROM vaults WHERE id = $1", vault_id)

    # POST-COMMIT, and deliberately ahead of the on-disk/role cleanup below so
    # it still runs if either of those fails: the vault-skill cache is keyed on
    # vault NAME, so a same-named recreate within the cache TTL would otherwise
    # serve the DELETED vault's skill body to the new vault's readers.
    # Over-invalidating costs one re-fetch; under-invalidating is a
    # cross-vault disclosure.
    try:
        from app.services import vault_skill_service

        vault_skill_service.invalidate(vault_name)
    except Exception as e:  # noqa: BLE001 — never fail a delete over a cache pop
        logger.warning("vault_skill cache invalidate failed for %s: %s", vault_name, e)

    # Legacy on-disk cleanup: bare repo + persistent worktree. Both must go,
    # otherwise a same-named recreate hits stale state on its second commit.
    # Native-ledger sentinel vaults have no filesystem authority, so even
    # constructing GitService would create unwanted storage directories.
    #
    # Commit executor, NOT inline: cleanup_vault_dirs blocks on the
    # per-vault threading.Lock and then rmtree's the whole repo —
    # running that on the event loop stalls every request (and /livez)
    # for the duration; running it on the shared to_thread pool lets a
    # lock wait eat a thread reads need. No write-lane gate on purpose:
    # PG cascade above already committed, and a lane-timeout 429 here
    # would strand orphan dirs that block a same-named recreate.
    # must_complete: the DB rows are gone — if a client disconnect
    # cancelled us while queueing for a slot, the dirs would otherwise
    # survive as orphans this request can never come back to repair.
    # A cancellation surfaced AFTER the cleanup completed must not skip
    # the role cleanup below either — park it, finish, then re-raise.
    pending_cancel: BaseException | None = None
    if not is_native_ledger_vault:
        from app.services.git_service import GitService

        try:
            await run_git_write(GitService().cleanup_vault_dirs, vault_name, must_complete=True)
        except asyncio.CancelledError as ce:
            pending_cancel = ce  # cleanup DID complete (must_complete)

    # PG-native RBAC: drop the three vault group roles. Memberships
    # auto-clean as part of DROP ROLE. run_compensation: a cancel mid-DDL
    # is absorbed until the drop COMPLETES — a bare shield would detach it
    # and strand the roles if shutdown closes the pool underneath.
    try:
        await run_compensation(get_role_sync().on_vault_delete(vault_id))
    except asyncio.CancelledError as ce:
        pending_cancel = ce

    logger.info("Deleted vault: %s", vault_name)
    if pending_cancel is not None:
        raise pending_cancel
    return {"deleted": True, "vault": vault_name}


# ── Destructive: user self-delete ──────────────────────────


async def delete_user_account(user_id: str) -> dict:
    """Delete the caller's account and everything they solely own.

    Order:
      1. Delete each owned vault via `delete_vault` (full cascade).
      2. Clear residual FK references from other vaults this user may have
         touched: vault_access grants they made, publications they created.
         SET NULL rather than deleting the artifacts — those belong to other
         users' vaults.
      3. DELETE users row. CASCADE clears `tokens` and `vault_access`
         rows keyed on user_id.

    Both SET NULL targets are declared nullable. A third one used to live
    here — `todos.assignee_id` / `.created_by` — against columns declared
    NOT NULL; since this block has no transaction wrapper, the two updates
    above committed and the NotNullViolationError then skipped the DELETE
    below, so the account could never be deleted. The `todos` stack was
    removed in migration 050 (see the guards in
    tests/test_todos_surface_removed_unit.py).
    """
    uid = uuid.UUID(user_id)
    pool = await get_pool()

    async with pool.acquire() as conn:
        protected = await conn.fetchrow(
            "SELECT is_recovery_admin, password_hash FROM users WHERE id = $1",
            uid,
        )
        if protected is not None and (
            protected["is_recovery_admin"]
            or is_retired_recovery_admin_password(protected["password_hash"])
        ):
            raise RecoveryAdminProtectedError()
        owned_vault_names = [
            r["name"] for r in await conn.fetch(
                "SELECT name FROM vaults WHERE owner_id = $1", uid
            )
        ]

    deleted_vaults: list[str] = []
    for vname in owned_vault_names:
        try:
            await delete_vault(user_id, vname)
            deleted_vaults.append(vname)
        except Exception as e:  # noqa: BLE001
            logger.warning("User %s delete_vault(%s) failed: %s", user_id, vname, e)

    async with pool.acquire() as conn:
        # Detach residual references rather than deleting the artifacts
        await conn.execute("UPDATE vault_access SET granted_by = NULL WHERE granted_by = $1", uid)
        await conn.execute("UPDATE publications SET created_by = NULL WHERE created_by = $1", uid)
        # CASCADE handles tokens + vault_access.user_id
        await conn.execute("DELETE FROM users WHERE id = $1", uid)

    # PG-native RBAC: drop akb_user_<uid>. Owned vault group roles
    # were already dropped by the per-vault delete_vault calls above.
    await get_role_sync().on_user_delete(uid)

    logger.info("Deleted user %s (vaults=%d)", user_id, len(deleted_vaults))
    return {"deleted": True, "user_id": user_id, "vaults_deleted": deleted_vaults}
