"""Low-level PostgreSQL primitives for the native Resource/Revision ledger."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Protocol

import asyncpg

from app.exceptions import ConflictError


class NativeRevisionIdCollisionError(ConflictError):
    """The server-generated opaque token collided with an existing Revision."""

    def __init__(self):
        super().__init__("Server-generated native Revision ID collision; retry the mutation")


class PreparedPayload(Protocol):
    @property
    def payload_id(self) -> uuid.UUID: ...

    @property
    def content_profile(self) -> str: ...

    @property
    def digest(self) -> str: ...

    @property
    def byte_size(self) -> int: ...

    @property
    def encoding(self) -> str: ...

    @property
    def selected_placement(self) -> str: ...

    @property
    def verification_profile(self) -> str: ...


class NativeRevisionRepository:
    """Repository operations that compose inside one caller-owned transaction."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @staticmethod
    async def lock_mutation(
        conn: asyncpg.Connection,
        namespace_id: uuid.UUID,
        mutation_id: uuid.UUID,
    ) -> None:
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            f"native-mutation:{namespace_id}:{mutation_id}",
        )

    @staticmethod
    async def lock_paths(
        conn: asyncpg.Connection,
        namespace_id: uuid.UUID,
        surface: str,
        *paths: str,
    ) -> None:
        """Lock namespace-wide path ownership in lexical order.

        ``surface`` remains in the call shape so callers cannot accidentally
        omit their Resource axis, but it is deliberately absent from the lock
        identity: a Document and File cannot concurrently own the same path.
        """
        del surface
        for path in sorted(set(paths)):
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"native-path:{namespace_id}:{path}",
            )

    @staticmethod
    async def find_mutation(
        conn: asyncpg.Connection,
        namespace_id: uuid.UUID,
        mutation_id: uuid.UUID,
    ) -> dict | None:
        row = await conn.fetchrow(
            """
            SELECT r.revision_id, r.resource_id, r.parent_revision_id,
                   r.action, r.path_at_revision AS path, r.request_fingerprint,
                   r.payload_manifest_id, r.occurred_at
              FROM native_revisions r
             WHERE r.namespace_id = $1 AND r.mutation_id = $2
            """,
            namespace_id,
            mutation_id,
        )
        return dict(row) if row is not None else None

    @staticmethod
    async def find_live_path(
        conn: asyncpg.Connection,
        namespace_id: uuid.UUID,
        surface: str,
        path: str,
        *,
        for_update: bool = False,
    ) -> dict | None:
        suffix = " FOR UPDATE" if for_update else ""
        row = await conn.fetchrow(
            """
            SELECT resource_id, namespace_id, surface, content_profile,
                   current_path, lifecycle, head_revision_id,
                   created_at, updated_at
              FROM native_resources
             WHERE namespace_id = $1
               AND surface = $2
               AND current_path = $3
               AND lifecycle = 'live'
            """
            + suffix,
            namespace_id,
            surface,
            path,
        )
        return dict(row) if row is not None else None

    async def resolve_live_reference(
        self,
        *,
        namespace_id: uuid.UUID,
        surface: str,
        reference: str,
        conn: asyncpg.Connection | None = None,
    ) -> dict | None:
        """Resolve a current path first, then a live old-path alias.

        Exact current ownership always wins.  The alias points directly to a
        Resource (never another path), so a chain cannot form after repeated
        moves and a deleted Resource cannot be resurrected by an old alias.
        """
        sql = """
            SELECT rs.resource_id, rs.namespace_id, rs.surface,
                   rs.content_profile, rs.current_path, rs.lifecycle,
                   rs.head_revision_id, rs.created_at, rs.updated_at
              FROM native_resources rs
             WHERE rs.namespace_id = $1
               AND rs.surface = $2
               AND rs.lifecycle = 'live'
               AND (
                    rs.current_path = $3
                    OR (
                        rs.current_path <> $3
                        AND EXISTS (
                            SELECT 1
                              FROM native_resource_path_aliases a
                             WHERE a.namespace_id = rs.namespace_id
                               AND a.surface = rs.surface
                               AND a.resource_id = rs.resource_id
                               AND a.old_path = $3
                               AND a.retired_revision_id IS NULL
                        )
                    )
               )
             ORDER BY (rs.current_path = $3) DESC
             LIMIT 1
        """
        if conn is not None:
            row = await conn.fetchrow(sql, namespace_id, surface, reference)
        else:
            async with self.pool.acquire() as acquired:
                row = await acquired.fetchrow(sql, namespace_id, surface, reference)
        return dict(row) if row is not None else None

    @staticmethod
    async def lock_resource(
        conn: asyncpg.Connection,
        resource_id: uuid.UUID,
    ) -> dict | None:
        row = await conn.fetchrow(
            """
            SELECT resource_id, namespace_id, surface, content_profile,
                   current_path, lifecycle, head_revision_id,
                   created_at, updated_at
              FROM native_resources
             WHERE resource_id = $1
             FOR UPDATE
            """,
            resource_id,
        )
        return dict(row) if row is not None else None

    @staticmethod
    async def insert_resource(
        conn: asyncpg.Connection,
        *,
        resource_id: uuid.UUID,
        namespace_id: uuid.UUID,
        surface: str,
        content_profile: str,
        path: str,
        occurred_at: datetime,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO native_resources (
                resource_id, namespace_id, surface, content_profile,
                current_path, lifecycle, created_at, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, 'live', $6, $6)
            """,
            resource_id,
            namespace_id,
            surface,
            content_profile,
            path,
            occurred_at,
        )

    @staticmethod
    async def insert_manifest(
        conn: asyncpg.Connection,
        *,
        payload_manifest_id: uuid.UUID,
        namespace_id: uuid.UUID,
        resource_id: uuid.UUID,
        payload: PreparedPayload,
        occurred_at: datetime,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO native_payload_manifests (
                payload_manifest_id, namespace_id, resource_id, content_profile,
                digest, byte_size, encoding, selected_placement, private_locator,
                verification_profile, created_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
            payload_manifest_id,
            namespace_id,
            resource_id,
            payload.content_profile,
            payload.digest,
            payload.byte_size,
            payload.encoding,
            payload.selected_placement,
            payload.payload_id,
            payload.verification_profile,
            occurred_at,
        )

    @staticmethod
    async def insert_revision(
        conn: asyncpg.Connection,
        *,
        revision_id: str,
        namespace_id: uuid.UUID,
        resource_id: uuid.UUID,
        parent_revision_id: str | None,
        action: str,
        path: str,
        path_from: str | None,
        path_to: str | None,
        payload_manifest_id: uuid.UUID | None,
        mutation_id: uuid.UUID,
        request_fingerprint: str,
        message: str | None,
        subject: str | None,
        summary: str | None,
        actor: str,
        occurred_at: datetime,
        activity_event_id: uuid.UUID,
        invalidation_intent_id: uuid.UUID,
    ) -> None:
        try:
            await conn.execute(
                """
                INSERT INTO native_revisions (
                    revision_id, namespace_id, resource_id, parent_revision_id,
                    action, path_at_revision, path_from, path_to,
                    payload_manifest_id, mutation_id, request_fingerprint,
                    message, subject, summary, actor, occurred_at,
                    activity_event_id, invalidation_intent_id
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9,
                    $10, $11, $12, $13, $14, $15, $16, $17, $18
                )
                """,
                revision_id,
                namespace_id,
                resource_id,
                parent_revision_id,
                action,
                path,
                path_from,
                path_to,
                payload_manifest_id,
                mutation_id,
                request_fingerprint,
                message,
                subject,
                summary,
                actor,
                occurred_at,
                activity_event_id,
                invalidation_intent_id,
            )
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name in {
                "native_revisions_pkey",
                "native_revisions_resource_revision_key",
                "native_revisions_namespace_resource_revision_key",
            }:
                raise NativeRevisionIdCollisionError() from exc
            raise

    @staticmethod
    async def copy_manifest(
        conn: asyncpg.Connection,
        *,
        source_manifest_id: uuid.UUID,
        payload_manifest_id: uuid.UUID,
        resource_id: uuid.UUID,
        occurred_at: datetime,
    ) -> None:
        """Pin the same verified payload facts in a new immutable manifest."""
        status = await conn.execute(
            """
            INSERT INTO native_payload_manifests (
                payload_manifest_id, namespace_id, resource_id, content_profile,
                digest, byte_size, encoding, selected_placement, private_locator,
                verification_profile, created_at
            )
            SELECT $2, namespace_id, $3, content_profile, digest, byte_size,
                   encoding, selected_placement, private_locator,
                   verification_profile, $4
              FROM native_payload_manifests
             WHERE payload_manifest_id = $1
            """,
            source_manifest_id,
            payload_manifest_id,
            resource_id,
            occurred_at,
        )
        if status != "INSERT 0 1":
            raise LookupError(f"Native payload manifest is missing: {source_manifest_id}")

    @staticmethod
    async def set_head(
        conn: asyncpg.Connection,
        *,
        resource_id: uuid.UUID,
        revision_id: str,
        path: str,
        lifecycle: str,
        occurred_at: datetime,
    ) -> None:
        await conn.execute(
            """
            UPDATE native_resources
               SET head_revision_id = $2,
                   current_path = $3,
                   lifecycle = $4,
                   updated_at = $5
             WHERE resource_id = $1
            """,
            resource_id,
            revision_id,
            path,
            lifecycle,
            occurred_at,
        )

    @staticmethod
    async def insert_activity(
        conn: asyncpg.Connection,
        *,
        activity_event_id: uuid.UUID,
        namespace_id: uuid.UUID,
        resource_id: uuid.UUID,
        revision_id: str,
        action: str,
        actor: str,
        subject: str | None,
        summary: str | None,
        path_from: str | None,
        path_to: str | None,
        occurred_at: datetime,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO native_revision_activity (
                activity_event_id, namespace_id, resource_id, revision_id,
                action, actor, subject, summary, changed_path_from,
                changed_path_to, occurred_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
            activity_event_id,
            namespace_id,
            resource_id,
            revision_id,
            action,
            actor,
            subject,
            summary,
            path_from,
            path_to,
            occurred_at,
        )

    @staticmethod
    async def insert_invalidation_intent(
        conn: asyncpg.Connection,
        *,
        intent_id: uuid.UUID,
        namespace_id: uuid.UUID,
        resource_id: uuid.UUID,
        revision_id: str,
        reason: str,
        occurred_at: datetime,
        selected_delivery: str | None = None,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO native_invalidation_intents (
                intent_id, namespace_id, resource_id, revision_id,
                reason, selected_delivery, occurred_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            intent_id,
            namespace_id,
            resource_id,
            revision_id,
            reason,
            selected_delivery,
            occurred_at,
        )

    @staticmethod
    async def insert_path_alias(
        conn: asyncpg.Connection,
        *,
        namespace_id: uuid.UUID,
        surface: str,
        old_path: str,
        resource_id: uuid.UUID,
        created_revision_id: str,
        occurred_at: datetime,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO native_resource_path_aliases (
                namespace_id, surface, old_path, resource_id,
                created_revision_id, created_at
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            namespace_id,
            surface,
            old_path,
            resource_id,
            created_revision_id,
            occurred_at,
        )

    @staticmethod
    async def retire_live_alias(
        conn: asyncpg.Connection,
        *,
        namespace_id: uuid.UUID,
        surface: str,
        old_path: str,
        retired_revision_id: str,
        occurred_at: datetime,
    ) -> None:
        await conn.execute(
            """
            UPDATE native_resource_path_aliases
               SET retired_revision_id = $4, retired_at = $5
             WHERE namespace_id = $1
               AND surface = $2
               AND old_path = $3
               AND retired_revision_id IS NULL
            """,
            namespace_id,
            surface,
            old_path,
            retired_revision_id,
            occurred_at,
        )

    @staticmethod
    async def retire_resource_aliases(
        conn: asyncpg.Connection,
        *,
        namespace_id: uuid.UUID,
        surface: str,
        resource_id: uuid.UUID,
        retired_revision_id: str,
        occurred_at: datetime,
    ) -> None:
        await conn.execute(
            """
            UPDATE native_resource_path_aliases
               SET retired_revision_id = $4, retired_at = $5
             WHERE namespace_id = $1
               AND surface = $2
               AND resource_id = $3
               AND retired_revision_id IS NULL
            """,
            namespace_id,
            surface,
            resource_id,
            retired_revision_id,
            occurred_at,
        )

    async def get_current(
        self,
        *,
        namespace_id: uuid.UUID,
        surface: str,
        path: str,
        conn: asyncpg.Connection | None = None,
    ) -> dict | None:
        sql = """
            SELECT rs.resource_id, rs.namespace_id, rs.surface,
                   rs.content_profile, rs.current_path AS path, rs.lifecycle,
                   rs.created_at AS resource_created_at,
                   rs.updated_at AS resource_updated_at,
                   rv.revision_id, rv.parent_revision_id, rv.action,
                   rv.occurred_at, pm.payload_manifest_id, pm.digest,
                   pm.byte_size, pm.encoding, pm.selected_placement,
                   pm.private_locator, pm.verification_profile
              FROM native_resources rs
              JOIN native_revisions rv
                ON rv.resource_id = rs.resource_id
               AND rv.revision_id = rs.head_revision_id
              LEFT JOIN native_payload_manifests pm
                ON pm.payload_manifest_id = rv.payload_manifest_id
             WHERE rs.namespace_id = $1
               AND rs.surface = $2
               AND rs.current_path = $3
               AND rs.lifecycle = 'live'
        """
        if conn is not None:
            row = await conn.fetchrow(sql, namespace_id, surface, path)
        else:
            async with self.pool.acquire() as acquired:
                row = await acquired.fetchrow(sql, namespace_id, surface, path)
        return dict(row) if row is not None else None

    async def get_resource_head(
        self,
        *,
        resource_id: uuid.UUID,
        conn: asyncpg.Connection | None = None,
    ) -> dict | None:
        sql = """
            SELECT rs.resource_id, rs.namespace_id, rs.surface,
                   rs.content_profile, rs.current_path AS path, rs.lifecycle,
                   rs.created_at AS resource_created_at,
                   rs.updated_at AS resource_updated_at,
                   rv.revision_id, rv.parent_revision_id, rv.action,
                   rv.occurred_at, pm.payload_manifest_id, pm.digest,
                   pm.byte_size, pm.encoding, pm.selected_placement,
                   pm.private_locator, pm.verification_profile
              FROM native_resources rs
              JOIN native_revisions rv
                ON rv.resource_id = rs.resource_id
               AND rv.revision_id = rs.head_revision_id
              LEFT JOIN native_payload_manifests pm
                ON pm.payload_manifest_id = rv.payload_manifest_id
             WHERE rs.resource_id = $1
        """
        if conn is not None:
            row = await conn.fetchrow(sql, resource_id)
        else:
            async with self.pool.acquire() as acquired:
                row = await acquired.fetchrow(sql, resource_id)
        return dict(row) if row is not None else None

    async def get_revision(
        self,
        *,
        resource_id: uuid.UUID,
        revision_id: str,
        conn: asyncpg.Connection | None = None,
    ) -> dict | None:
        sql = """
            SELECT rv.*, rs.surface, rs.content_profile,
                   rs.created_at AS resource_created_at,
                   rs.updated_at AS resource_updated_at,
                   pm.digest, pm.byte_size, pm.encoding,
                   pm.selected_placement, pm.private_locator,
                   pm.verification_profile
              FROM native_revisions rv
              JOIN native_resources rs ON rs.resource_id = rv.resource_id
              LEFT JOIN native_payload_manifests pm
                ON pm.payload_manifest_id = rv.payload_manifest_id
             WHERE rv.resource_id = $1 AND rv.revision_id = $2
        """
        if conn is not None:
            row = await conn.fetchrow(sql, resource_id, revision_id)
        else:
            async with self.pool.acquire() as acquired:
                row = await acquired.fetchrow(sql, resource_id, revision_id)
        return dict(row) if row is not None else None

    async def list_history(
        self,
        *,
        resource_id: uuid.UUID,
        limit: int = 100,
        conn: asyncpg.Connection | None = None,
    ) -> list[dict]:
        sql = """
            SELECT revision_id, resource_id, parent_revision_id, action,
                   path_at_revision AS path, path_from, path_to,
                   payload_manifest_id, message, subject, summary, actor,
                   occurred_at
              FROM native_revisions
             WHERE resource_id = $1
             ORDER BY occurred_at DESC, revision_id DESC
             LIMIT $2
        """
        if conn is not None:
            rows = await conn.fetch(sql, resource_id, limit)
        else:
            async with self.pool.acquire() as acquired:
                rows = await acquired.fetch(sql, resource_id, limit)
        return [dict(row) for row in rows]

    async def list_activity(
        self,
        *,
        namespace_id: uuid.UUID,
        limit: int,
        since: datetime | None = None,
        path: str | None = None,
    ) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT a.resource_id, a.revision_id, a.action, a.actor,
                       a.subject, a.summary, a.changed_path_from,
                       a.changed_path_to, a.occurred_at,
                       r.path_at_revision
                  FROM native_revision_activity a
                  JOIN native_revisions r
                    ON r.revision_id = a.revision_id
                 WHERE a.namespace_id = $1
                   AND ($2::timestamptz IS NULL OR a.occurred_at >= $2)
                   AND (
                        $3::text IS NULL
                        OR r.path_at_revision = $3
                        OR r.path_at_revision LIKE $3 || '/%'
                        OR a.changed_path_from = $3
                        OR a.changed_path_from LIKE $3 || '/%'
                        OR a.changed_path_to = $3
                        OR a.changed_path_to LIKE $3 || '/%'
                   )
                 ORDER BY a.occurred_at DESC, a.revision_id DESC
                 LIMIT $4
                """,
                namespace_id,
                since,
                path,
                limit,
            )
        return [dict(row) for row in rows]

    async def list_recent_document_heads(
        self,
        *,
        user_id: uuid.UUID | None,
        vault: str | None,
        limit: int,
    ) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT rs.resource_id, rs.current_path AS path,
                       rs.head_revision_id AS revision_id, rs.updated_at,
                       v.name AS vault_name, p.canonical_bytes
                  FROM native_resources rs
                  JOIN vaults v ON v.id = rs.namespace_id
                  JOIN native_revisions rv
                    ON rv.resource_id = rs.resource_id
                   AND rv.revision_id = rs.head_revision_id
                  JOIN native_payload_manifests pm
                    ON pm.payload_manifest_id = rv.payload_manifest_id
                  JOIN m1_reference_payloads p
                    ON p.payload_id = pm.private_locator
                  LEFT JOIN vault_access va
                    ON va.vault_id = v.id AND va.user_id = $1
                 WHERE rs.surface = 'document'
                   AND rs.lifecycle = 'live'
                   AND ($2::text IS NULL OR v.name = $2)
                   AND (
                        $2::text IS NOT NULL
                        OR v.owner_id = $1
                        OR va.user_id = $1
                        OR v.public_access IN ('reader', 'writer')
                   )
                 ORDER BY rs.updated_at DESC, rs.resource_id DESC
                 LIMIT $3
                """,
                user_id,
                vault,
                limit,
            )
        return [dict(row) for row in rows]

    @staticmethod
    async def find_live_alias(
        conn: asyncpg.Connection,
        *,
        namespace_id: uuid.UUID,
        surface: str,
        old_path: str,
    ) -> dict | None:
        row = await conn.fetchrow(
            """
            SELECT alias_id, namespace_id, surface, old_path, resource_id,
                   created_revision_id, created_at
              FROM native_resource_path_aliases
             WHERE namespace_id = $1
               AND surface = $2
               AND old_path = $3
               AND retired_revision_id IS NULL
            """,
            namespace_id,
            surface,
            old_path,
        )
        return dict(row) if row is not None else None
