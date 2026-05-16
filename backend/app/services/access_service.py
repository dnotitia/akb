"""Access control service — vault roles, grant/revoke, permission checks.

Role hierarchy: owner > admin > writer > reader > (none)
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from app.db.postgres import get_pool
from app.exceptions import ForbiddenError, NotFoundError, ValidationError

logger = logging.getLogger("akb.access")

ROLE_HIERARCHY = {"owner": 4, "admin": 3, "writer": 2, "reader": 1}
VALID_ROLES = set(ROLE_HIERARCHY.keys())
VALID_PUBLIC_ACCESS = {"none", "reader", "writer"}


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

async def check_vault_access(user_id: str, vault_name: str, required_role: str = "reader") -> dict:
    """Check if user has at least the required role on a vault.

    Returns vault info dict if authorized.
    Raises ForbiddenError if not.
    Raises NotFoundError if vault doesn't exist.
    """
    pool = await get_pool()
    uid = uuid.UUID(user_id)

    async with pool.acquire() as conn:
        vault = await conn.fetchrow("SELECT id, name, owner_id, status, public_access FROM vaults WHERE name = $1", vault_name)
        if not vault:
            raise NotFoundError("Vault", vault_name)

        # Check archived vault FIRST — even admin/owner can't write to archived
        if vault["status"] == "archived" and required_role in ("writer",):
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

        # System admin bypasses all vault ACL
        is_admin = await conn.fetchval("SELECT is_admin FROM users WHERE id = $1", uid)
        if is_admin:
            return {"vault_id": vault["id"], "role": "owner", "status": vault["status"], "role_source": "member"}

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

    # Verify granter has permission
    await check_vault_access(granter_id, vault_name, required_role="admin")

    pool = await get_pool()
    async with pool.acquire() as conn:
        vault = await conn.fetchrow("SELECT id FROM vaults WHERE name = $1", vault_name)
        target = await conn.fetchrow("SELECT id, username FROM users WHERE username = $1", target_username)
        if not target:
            raise NotFoundError("User", target_username)

        # Upsert access
        await conn.execute(
            """
            INSERT INTO vault_access (id, vault_id, user_id, role, granted_by)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (vault_id, user_id)
            DO UPDATE SET role = $4, granted_by = $5
            """,
            uuid.uuid4(), vault["id"], target["id"], role, uuid.UUID(granter_id),
        )

    logger.info("Granted %s role to %s on vault %s", role, target_username, vault_name)
    return {"vault": vault_name, "user": target_username, "role": role, "granted": True}


async def revoke_access(revoker_id: str, vault_name: str, target_username: str) -> dict:
    """Revoke vault access from a user. Revoker must be owner or admin."""
    await check_vault_access(revoker_id, vault_name, required_role="admin")

    pool = await get_pool()
    async with pool.acquire() as conn:
        vault = await conn.fetchrow("SELECT id, owner_id FROM vaults WHERE name = $1", vault_name)
        target = await conn.fetchrow("SELECT id FROM users WHERE username = $1", target_username)
        if not target:
            raise NotFoundError("User", target_username)

        # Can't revoke owner
        if vault["owner_id"] == target["id"]:
            raise ForbiddenError("Cannot revoke owner's access. Use transfer_ownership instead.")

        result = await conn.execute(
            "DELETE FROM vault_access WHERE vault_id = $1 AND user_id = $2",
            vault["id"], target["id"],
        )

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

        if is_admin:
            rows = await conn.fetch(
                """
                SELECT v.id, v.name, v.description, v.status, v.created_at,
                       COALESCE(CASE WHEN v.owner_id = $1 THEN 'owner' END, 'admin') as role
                FROM vaults v
                ORDER BY v.name
                """,
                uid,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT v.id, v.name, v.description, v.status, v.created_at,
                       COALESCE(va.role, CASE WHEN v.owner_id = $1 THEN 'owner' WHEN v.public_access != 'none' THEN v.public_access END) as role
                FROM vaults v
                LEFT JOIN vault_access va ON v.id = va.vault_id AND va.user_id = $1
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
    (
        owner,
        member_count,
        doc_count,
        table_count,
        file_count,
        edge_count,
        last_doc,
        is_external_git,
    ) = await asyncio.gather(
        _r("SELECT username, display_name FROM users WHERE id = $1", vault["owner_id"]),
        _q("SELECT COUNT(*) FROM vault_access WHERE vault_id = $1", vid),
        _q("SELECT COUNT(*) FROM documents WHERE vault_id = $1", vid),
        _q("SELECT COUNT(*) FROM vault_tables WHERE vault_id = $1", vid),
        _q("SELECT COUNT(*) FROM vault_files WHERE vault_id = $1", vid),
        _q("SELECT COUNT(*) FROM edges WHERE vault_id = $1", vid),
        _r(
            "SELECT updated_at, created_by FROM documents WHERE vault_id = $1 "
            "ORDER BY updated_at DESC LIMIT 1",
            vid,
        ),
        _q("SELECT 1 FROM vault_external_git WHERE vault_id = $1", vid),
    )

    tables = await _list_tables_with_schema(vault_name, vid) if table_count else []

    return {
        "name": vault["name"],
        "description": vault["description"],
        "status": vault["status"],
        "is_archived": vault["status"] == "archived",
        "is_external_git": bool(is_external_git),
        "public_access": vault["public_access"],
        "role": caller_role,
        "role_source": role_source,
        "owner": owner["username"] if owner else None,
        "owner_display_name": owner["display_name"] if owner else None,
        "member_count": member_count + 1,  # +1 for owner
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
            "SELECT id, name FROM vault_tables WHERE vault_id = $1 ORDER BY name",
            vault_id,
        )
        if not registry:
            return []

        # All columns for the vault's vt_* tables in one query — use the
        # canonical sanitizer so hyphenated vault names map to the actual
        # `vt_<sanitised>__<sanitised>` PG identifiers.
        from app.repositories.table_data_repo import pg_table_name
        pg_names = [pg_table_name(vault_name, r["name"]) for r in registry]
        # Map back PG name → registry short name for output.
        short_by_pg = {pg_table_name(vault_name, r["name"]): r["name"] for r in registry}
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
    """Transfer vault ownership. Only current owner can do this."""
    await check_vault_access(owner_id, vault_name, required_role="owner")

    pool = await get_pool()
    async with pool.acquire() as conn:
        vault = await conn.fetchrow("SELECT id, owner_id FROM vaults WHERE name = $1", vault_name)
        new_owner = await conn.fetchrow("SELECT id, username FROM users WHERE username = $1", new_owner_username)
        if not new_owner:
            raise NotFoundError("User", new_owner_username)

        # Update vault owner
        await conn.execute("UPDATE vaults SET owner_id = $1 WHERE id = $2", new_owner["id"], vault["id"])

        # Give old owner admin role
        await conn.execute(
            """
            INSERT INTO vault_access (id, vault_id, user_id, role, granted_by)
            VALUES ($1, $2, $3, 'admin', $4)
            ON CONFLICT (vault_id, user_id) DO UPDATE SET role = 'admin'
            """,
            uuid.uuid4(), vault["id"], vault["owner_id"], new_owner["id"],
        )

        # Remove new owner from vault_access (they're now owner via vaults.owner_id)
        await conn.execute(
            "DELETE FROM vault_access WHERE vault_id = $1 AND user_id = $2",
            vault["id"], new_owner["id"],
        )

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
            SELECT u.id, u.username, u.display_name, u.email, u.is_admin, u.created_at,
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

    logger.info("Updated vault metadata: %s", vault_name)
    return {"vault": vault_name, "updated": True}


# ── Destructive: vault delete ───────────────────────────────


async def delete_vault(user_id: str, vault_name: str) -> dict:
    """Permanently delete a vault and all its data. Owner or admin only.

    Cascades:
      - S3 file objects (via vault_files)
      - edges, chunks (via vector-store outbox + sync vector-store delete in
        index_service.delete_vault_chunks)
      - vault_tables (drops the underlying PG tables)
      - todos, sessions
      - documents, collections, vault_access
      - the vault row itself
      - git bare repo directory

    Extracted from the MCP `akb_delete_vault` handler so the REST
    self-delete endpoint can reuse the same path.
    """
    from app.config import settings
    from app.repositories import table_data_repo
    from app.services.git_service import GitService
    from app.services.index_service import delete_vault_chunks

    await check_vault_access(user_id, vault_name, required_role="admin")
    pool = await get_pool()
    async with pool.acquire() as conn:
        vault = await conn.fetchrow("SELECT id FROM vaults WHERE name = $1", vault_name)
        if not vault:
            return {"error": f"Vault not found: {vault_name}"}
        vault_id = vault["id"]

        # Delete S3 files before DB cascade removes vault_files records.
        file_rows = await conn.fetch("SELECT s3_key FROM vault_files WHERE vault_id = $1", vault_id)
        if file_rows and settings.s3_endpoint_url:
            from app.services.adapters import s3_adapter
            failed = []
            for fr in file_rows:
                try:
                    s3_adapter.delete(fr["s3_key"])
                except Exception as e:  # noqa: BLE001
                    failed.append(fr["s3_key"])
                    logger.warning("Failed to delete S3 object %s: %s", fr["s3_key"], e)
            if failed:
                logger.error("Vault %s: %d/%d S3 files failed to delete", vault_name, len(failed), len(file_rows))
            await conn.execute("DELETE FROM vault_files WHERE vault_id = $1", vault_id)

        await conn.execute("DELETE FROM edges WHERE vault_id = $1", vault_id)
        await delete_vault_chunks(conn, vault_id)

        vtables = await conn.fetch("SELECT name FROM vault_tables WHERE vault_id = $1", vault_id)
        for vt in vtables:
            pg_name = table_data_repo.pg_table_name(vault_name, vt["name"])
            await conn.execute(f"DROP TABLE IF EXISTS {pg_name}")
        await conn.execute("DELETE FROM vault_tables WHERE vault_id = $1", vault_id)

        await conn.execute("DELETE FROM todos WHERE vault_id = $1", vault_id)
        await conn.execute("DELETE FROM sessions WHERE vault_id = $1", vault_id)
        await conn.execute("DELETE FROM documents WHERE vault_id = $1", vault_id)
        await conn.execute("DELETE FROM collections WHERE vault_id = $1", vault_id)
        await conn.execute("DELETE FROM vault_access WHERE vault_id = $1", vault_id)
        await conn.execute("DELETE FROM vaults WHERE id = $1", vault_id)

    # On-disk cleanup: bare repo + persistent worktree. Both must go,
    # otherwise a same-named recreate hits stale state on its second
    # commit (the first that materialises the worktree).
    GitService().cleanup_vault_dirs(vault_name)

    logger.info("Deleted vault: %s", vault_name)
    return {"deleted": True, "vault": vault_name}


# ── Destructive: user self-delete ──────────────────────────


async def delete_user_account(user_id: str) -> dict:
    """Delete the caller's account and everything they solely own.

    Order:
      1. Delete each owned vault via `delete_vault` (full cascade).
      2. Clear residual FK references from other vaults this user may have
         touched: todos they authored/are assigned, vault_access grants they
         made, publications they created. SET NULL rather than deleting the
         artifacts — those belong to other users' vaults.
      3. DELETE users row. CASCADE clears `memories`, `tokens`, and
         `vault_access` rows keyed on user_id.
    """
    uid = uuid.UUID(user_id)
    pool = await get_pool()

    async with pool.acquire() as conn:
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
        await conn.execute("UPDATE todos SET assignee_id = NULL WHERE assignee_id = $1", uid)
        await conn.execute("UPDATE todos SET created_by = NULL WHERE created_by = $1", uid)
        # CASCADE handles memories, tokens, vault_access.user_id
        await conn.execute("DELETE FROM users WHERE id = $1", uid)

    logger.info("Deleted user %s (vaults=%d)", user_id, len(deleted_vaults))
    return {"deleted": True, "user_id": user_id, "vaults_deleted": deleted_vaults}
