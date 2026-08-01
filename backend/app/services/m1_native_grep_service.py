"""Head-pinned direct PostgreSQL grep candidate for M1 B-grep.

The service is deliberately internal and measurement-only.  It scans the
canonical PG BodyStore representation after applying vault ACL and containment
filters, then verifies every result against the exact Head bytes in Python.
Derived chunks, embeddings, and indexes are never result authority.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.exceptions import ForbiddenError, ValidationError
from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.native_revision_service import NativeRevisionService
from app.services.uri_service import doc_uri, file_uri


@dataclass(frozen=True, slots=True)
class HeadBody:
    namespace_id: uuid.UUID
    vault: str
    resource_id: uuid.UUID
    surface: str
    path: str
    revision_id: str
    digest: str
    byte_size: int
    canonical_bytes: bytes

    @property
    def text(self) -> str:
        return self.canonical_bytes.decode("utf-8", errors="strict")

    @property
    def uri(self) -> str:
        if self.surface == "document":
            return doc_uri(self.vault, self.path)
        return file_uri(self.vault, str(self.resource_id))


class M1NativeGrepService:
    """Direct scan/verify executor over current native Head bodies."""

    def __init__(self, pool: asyncpg.Pool, *, body_store: M1PgBodyStore | None = None):
        self.pool = pool
        self.body_store = body_store or M1PgBodyStore(pool)

    async def _head_bodies(
        self,
        *,
        user_id: uuid.UUID,
        vaults: list[str] | None,
        collection: str | None,
        resource_id: uuid.UUID | None,
    ) -> list[HeadBody]:
        conditions = [
            "rs.lifecycle = 'live'",
            "rs.content_profile = 'text'",
            "pm.selected_placement = 'pg-bodystore-v1'",
            "(v.owner_id = $1 OR EXISTS ("
            "SELECT 1 FROM vault_access va WHERE va.vault_id = v.id AND va.user_id = $1"
            ") OR v.public_access IN ('reader', 'writer'))",
        ]
        params: list[Any] = [user_id]
        if vaults:
            params.append(vaults)
            conditions.append(f"v.name = ANY(${len(params)})")
        if collection:
            prefix = collection.strip("/")
            escaped_prefix = (
                prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            params.extend([prefix, f"{escaped_prefix}/%"])
            conditions.append(
                f"(rs.current_path = ${len(params) - 1} OR "
                f"rs.current_path LIKE ${len(params)} ESCAPE '\\')"
            )
        if resource_id is not None:
            params.append(resource_id)
            conditions.append(f"rs.resource_id = ${len(params)}")

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT rs.namespace_id, v.name AS vault, rs.resource_id,
                       rs.surface, rs.current_path, rs.head_revision_id,
                       pm.digest, pm.byte_size, pm.encoding,
                       pm.selected_placement, pm.verification_profile,
                       p.canonical_bytes
                  FROM native_resources rs
                  JOIN vaults v ON v.id = rs.namespace_id
                  JOIN native_revisions nr
                    ON nr.resource_id = rs.resource_id
                   AND nr.revision_id = rs.head_revision_id
                  JOIN native_payload_manifests pm
                    ON pm.payload_manifest_id = nr.payload_manifest_id
                  JOIN m1_reference_payloads p
                    ON p.payload_id = pm.private_locator
                 WHERE {' AND '.join(conditions)}
                 ORDER BY v.name, rs.current_path, rs.resource_id
                """,
                *params,
            )

        bodies: list[HeadBody] = []
        for row in rows:
            canonical = M1PgBodyStore._verify_row(row)
            bodies.append(
                HeadBody(
                    namespace_id=row["namespace_id"],
                    vault=row["vault"],
                    resource_id=row["resource_id"],
                    surface=row["surface"],
                    path=row["current_path"],
                    revision_id=row["head_revision_id"],
                    digest=row["digest"],
                    byte_size=row["byte_size"],
                    canonical_bytes=canonical,
                )
            )
        return bodies

    async def _require_write_access(
        self,
        *,
        user_id: uuid.UUID,
        namespace_ids: set[uuid.UUID],
    ) -> None:
        if not namespace_ids:
            return
        async with self.pool.acquire() as conn:
            writable_rows = await conn.fetch(
                """
                SELECT v.id
                 FROM vaults v
                 WHERE v.id = ANY($1::uuid[])
                   AND v.status <> 'archived'
                   AND (
                       v.owner_id = $2
                       OR EXISTS (
                           SELECT 1 FROM users u
                            WHERE u.id = $2 AND u.is_admin = TRUE
                       )
                       OR EXISTS (
                           SELECT 1 FROM vault_access va
                            WHERE va.vault_id = v.id
                              AND va.user_id = $2
                              AND va.role IN ('writer', 'admin', 'owner')
                       )
                       OR v.public_access = 'writer'
                   )
                """,
                list(namespace_ids),
                user_id,
            )
        writable_ids = {row["id"] for row in writable_rows}
        if writable_ids != namespace_ids:
            raise ForbiddenError("grep replace requires writer access to every matched vault")

    @staticmethod
    def _matcher(pattern: str, *, regex: bool, case_sensitive: bool):
        if regex:
            try:
                compiled = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
            except re.error as exc:
                raise ValidationError(f"Invalid regex pattern: {exc}") from exc
            return lambda line: compiled.search(line) is not None
        if case_sensitive:
            return lambda line: pattern in line
        folded = pattern.casefold()
        return lambda line: folded in line.casefold()

    async def grep(
        self,
        pattern: str,
        *,
        user_id: uuid.UUID,
        vaults: list[str] | None = None,
        collection: str | None = None,
        resource_id: uuid.UUID | None = None,
        regex: bool = False,
        case_sensitive: bool = False,
        count_only: bool = False,
        files_with_matches: bool = False,
        limit: int = 20,
        replace: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        if count_only and files_with_matches:
            raise ValidationError("count_only and files_with_matches are mutually exclusive")
        if replace is not None and (count_only or files_with_matches):
            raise ValidationError("replace is incompatible with count_only/files_with_matches")
        if replace is not None and not actor:
            raise ValidationError("actor is required for native grep replacement")
        if limit < 1 or limit > 1000:
            raise ValidationError("limit must be between 1 and 1000")

        matcher = self._matcher(pattern, regex=regex, case_sensitive=case_sensitive)
        bodies = await self._head_bodies(
            user_id=user_id,
            vaults=vaults,
            collection=collection,
            resource_id=resource_id,
        )
        matched: list[dict[str, Any]] = []
        for body in bodies:
            lines = [
                {"line": number, "text": line.strip()}
                for number, line in enumerate(body.text.splitlines(), start=1)
                if matcher(line)
            ]
            if lines:
                matched.append(
                    {
                        "uri": body.uri,
                        "resource_type": body.surface,
                        "vault": body.vault,
                        "path": body.path,
                        "revision": body.revision_id,
                        "content_hash": body.digest,
                        "matches": lines,
                        "_body": body,
                    }
                )

        total_matches = sum(len(item["matches"]) for item in matched)
        base = {
            "pattern": pattern,
            "regex": regex,
            "searched_resources": len(bodies),
            "searched_bytes": sum(body.byte_size for body in bodies),
            "total_resources": len(matched),
            "total_matches": total_matches,
        }
        if count_only:
            return {
                **base,
                "by_resource": {item["uri"]: len(item["matches"]) for item in matched},
            }
        if files_with_matches:
            resources = [
                {k: item[k] for k in ("uri", "resource_type", "revision", "path")}
                for item in matched
            ]
            return {**base, "n_resources": len(resources), "resources": resources}

        selected = matched[:limit]
        replacements: list[dict[str, Any]] = []
        if replace is not None:
            await self._require_write_access(
                user_id=user_id,
                namespace_ids={item["_body"].namespace_id for item in selected},
            )
            native = NativeRevisionService(self.pool, payload_store=self.body_store)
            for item in selected:
                head_body: HeadBody = item["_body"]
                if regex:
                    flags = 0 if case_sensitive else re.IGNORECASE
                    new_text = re.sub(pattern, replace, head_body.text, flags=flags)
                elif case_sensitive:
                    new_text = head_body.text.replace(pattern, replace)
                else:
                    new_text = re.sub(re.escape(pattern), replace, head_body.text, flags=re.IGNORECASE)
                if new_text == head_body.text:
                    continue
                result = await native.replace_text(
                    namespace_id=head_body.namespace_id,
                    surface=head_body.surface,
                    path=head_body.path,
                    payload=new_text,
                    actor=actor or "",
                    mutation_id=uuid.uuid4(),
                    expected_revision_id=head_body.revision_id,
                    expected_resource_id=head_body.resource_id,
                    message=f"grep replace: {pattern!r}",
                )
                replacements.append({"uri": head_body.uri, "revision": result.revision_id})

        clean = [
            {key: value for key, value in item.items() if key != "_body"}
            for item in selected
        ]
        response: dict[str, Any] = {
            **base,
            "returned_resources": len(clean),
            "returned_matches": sum(len(item["matches"]) for item in clean),
            "truncated": len(matched) > len(clean),
            "results": clean,
        }
        if replace is not None:
            response.update(
                {"replace": replace, "replaced_resources": len(replacements), "replacements": replacements}
            )
        return response
