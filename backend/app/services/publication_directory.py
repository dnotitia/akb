"""Bounded, authenticated public-link inventory across readable Vaults.

The legacy per-Vault endpoint intentionally retains its complete-response
contract. This directory filters before keyset paging and rechecks visibility
on every page. Management capabilities are observations; mutations still pass
through the existing writer and managed-Vault authorization boundary.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import AKBError, ForbiddenError
from app.services.access_service import list_accessible_vaults
from app.services.auth_service import AuthenticatedUser, token_has_scope
from app.services.publication_service import _row_to_internal_dict, to_public_dict
from app.services.uri_service import doc_uri

Status = Literal["active", "all", "unavailable"]
Expiry = Literal["any", "none", "soon", "long-running"]


def _pack(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = hmac.new(settings.system_hmac_secret_effective.encode(), raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + signature).decode().rstrip("=")


def _unpack(cursor: str, binding: str) -> dict:
    try:
        if len(cursor) > 2048:
            raise ValueError
        data = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        raw, supplied = data[:-32], data[-32:]
        expected = hmac.new(settings.system_hmac_secret_effective.encode(), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, supplied):
            raise ValueError
        value = json.loads(raw)
        if value["v"] != 1 or value["binding"] != binding:
            raise ValueError
        observed = datetime.fromisoformat(value["at"])
        created = datetime.fromisoformat(value["created"])
        now = datetime.now(timezone.utc)
        if observed.tzinfo is None or created.tzinfo is None or not now - timedelta(minutes=30) <= observed <= now:
            raise ValueError
        if not isinstance(value["slug"], str) or not value["slug"] or len(value["slug"]) > 256:
            raise ValueError
        return {"at": observed, "created": created, "slug": value["slug"]}
    except (ValueError, TypeError, KeyError, UnicodeDecodeError):
        raise AKBError("Public-link results changed. Refresh the list to continue.", status_code=400) from None


def _restriction(user: AuthenticatedUser, vault: dict, *, system_admin: bool, mirror: bool, granted: bool) -> str | None:
    if vault["status"] == "archived":
        return "This Vault is archived and read-only."
    if mirror:
        return "This Vault is a read-only external Git mirror."
    if not token_has_scope(user.token_scopes, "write"):
        return "This credential does not allow writes."
    if user.vault_scope is not None and not user.vault_scope.permits(vault["name"]):
        return "This Vault is outside this credential's write scope."
    if vault.get("managed_by") is not None:
        if granted or (system_admin and user.vault_scope is None):
            return None
        return "This Vault requires a granted service token for writes."
    if vault["role"] not in ("writer", "admin", "owner"):
        return "Unpublish requires a Writer, Admin, or Owner role."
    return None


async def list_directory(
    user: AuthenticatedUser, *, vault: str | None = None, query: str = "",
    status: Status = "active", expiry: Expiry = "any", page_size: int = 30,
    resource_type: str | None = None, cursor: str | None = None,
) -> dict:
    query = query.strip()
    binding = hashlib.sha256(json.dumps(
        [user.user_id, user.token_id, vault, query, status, expiry, resource_type, page_size],
        ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()
    boundary = _unpack(cursor, binding) if cursor else None
    observed = boundary["at"] if boundary else datetime.now(timezone.utc)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            accessible = await list_accessible_vaults(user.user_id, conn=conn)
            selected = [item for item in accessible if vault is None or item["name"] == vault]
            if vault is not None and not selected:
                raise ForbiddenError("This Vault is unavailable or you no longer have access.")
            by_id = {item["id"]: item for item in selected}
            ids = [uuid.UUID(key) for key in by_id]
            # Native identity and its current path are authoritative. Do not
            # obtain a title by resolving an old, reusable document URI.
            base = """
                WITH inventory AS (
                    SELECT p.*, v.name AS vault,
                           COALESCE(NULLIF(btrim(p.title), ''), NULLIF(d.title, ''),
                                    NULLIF(f.name, ''), NULLIF(regexp_replace(n.current_path, '^.*/', ''), ''),
                                    NULLIF(regexp_replace(p.resource_uri, '^.*/', ''), ''), p.slug) AS display_title,
                           COALESCE(n.current_path, d.path) AS document_path,
                           COALESCE(n.current_path, d.path, p.resource_uri, '') AS resource_path,
                           CASE WHEN p.expires_at <= $2 THEN 'expired'
                                WHEN p.max_views IS NOT NULL AND p.view_count >= p.max_views THEN 'view-limit-reached'
                                ELSE 'active' END AS availability
                      FROM publications p JOIN vaults v ON v.id = p.vault_id
                      LEFT JOIN documents d ON d.id = p.document_id AND d.vault_id = p.vault_id
                      LEFT JOIN native_resources n ON n.resource_id = p.native_document_id
                           AND n.namespace_id = p.vault_id AND n.surface = 'document' AND n.lifecycle = 'live'
                      LEFT JOIN vault_files f ON p.resource_type = 'file' AND f.vault_id = p.vault_id
                           AND p.resource_uri = 'akb://' || v.name || '/file/' || f.id::text
                     WHERE p.vault_id = ANY($1::uuid[]) AND p.created_at <= $2
                )
            """
            args: list = [ids, observed]
            predicates = []
            if status != "all":
                predicates.append("availability = 'active'" if status == "active" else "availability <> 'active'")
            if expiry == "none":
                predicates.append("expires_at IS NULL")
            elif expiry == "soon":
                predicates.append("expires_at > $2 AND expires_at <= $2 + INTERVAL '7 days'")
            elif expiry == "long-running":
                predicates.append("availability = 'active' AND created_at <= $2 - INTERVAL '30 days'")
            if resource_type:
                args.append(resource_type)
                predicates.append(f"resource_type = ${len(args)}")
            if query:
                literal = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                args.append(f"%{literal}%")
                predicates.append(f"concat_ws(' ', display_title, resource_path, slug, vault) ILIKE ${len(args)}")
            where = " AND ".join(predicates) or "TRUE"
            total = await conn.fetchval(base + "SELECT count(*) FROM inventory WHERE " + where, *args)
            if boundary:
                args.extend([boundary["created"], boundary["slug"]])
                where += f" AND (created_at, slug) < (${len(args)-1}, ${len(args)})"
            args.append(page_size + 1)
            rows = await conn.fetch(
                base + "SELECT * FROM inventory WHERE " + where
                + f" ORDER BY created_at DESC, slug DESC LIMIT ${len(args)}", *args,
            )
            page = rows[:page_size]
            page_vaults = list({row["vault_id"] for row in page})
            mirrors = {row["vault_id"] for row in await conn.fetch(
                "SELECT vault_id FROM vault_external_git WHERE vault_id=ANY($1::uuid[])", page_vaults,
            )} if page else set()
            granted = set()
            if page and user.token_id:
                granted = {row["vault_id"] for row in await conn.fetch(
                    "SELECT vault_id FROM vault_write_grants WHERE token_id=$1 AND vault_id=ANY($2::uuid[]) "
                    "AND '*'=ANY(write_actions)", uuid.UUID(user.token_id), page_vaults,
                )}
            system_admin = bool(await conn.fetchval("SELECT is_admin FROM users WHERE id=$1", uuid.UUID(user.user_id)))
            items = []
            for row in page:
                item = to_public_dict(_row_to_internal_dict(row))
                if row["resource_type"] == "document" and row["document_path"]:
                    item["resource_uri"] = doc_uri(row["vault"], row["document_path"])
                reason = _restriction(user, by_id[str(row["vault_id"])], system_admin=system_admin,
                                      mirror=row["vault_id"] in mirrors, granted=row["vault_id"] in granted)
                item.update(title=row["display_title"], resource_path=row["resource_path"],
                            availability=row["availability"], can_unpublish=reason is None,
                            unpublish_disabled_reason=reason)
                items.append(item)
    next_cursor = None
    if len(rows) > page_size:
        last = page[-1]
        next_cursor = _pack({"v": 1, "binding": binding, "at": observed.isoformat(),
                             "created": last["created_at"].isoformat(), "slug": last["slug"]})
    return {"publications": items, "total": total, "complete": next_cursor is None,
            "next_cursor": next_cursor, "observed_at": observed.isoformat()}
