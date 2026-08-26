"""Source-keyed grant contributions behind the effective ``vault_access`` row.

``vault_access`` stores the result of a grant, not the grant: one row per
(vault, user), and ``grant_access`` overwrites ``role`` on conflict. That is
correct while every grant is one person acting on another — there is only ever
one reason then — and it stops being correct the moment a second, automated
grantor exists, because its revoke deletes access that was never its to remove.

This migration adds the place a reason can live. It changes no effective role:
every existing row is backfilled as exactly one contribution with source key
``direct``, preserving ``role``, ``granted_by`` and ``created_at``. The derived
value therefore equals the stored value for every pair, and every reader — REST,
``akb_sql``, search, grep, revision visibility, and the PostgreSQL role
membership ``role_sync`` reconciles out of ``vault_access`` — sees exactly what
it saw before. The check at the end is what proves that rather than assuming it.

Ownership, ``public_access``, system administration and the vault write policy
stay outside this table: ``check_vault_access`` decides them on separate
branches and reports them through ``role_source``. ``role`` therefore admits
``admin`` — which ``transfer_ownership`` writes for a former owner — and never
``owner``.
"""

from __future__ import annotations

import logging


logger = logging.getLogger("akb.migration.085")


async def migrate(conn) -> None:
    async with conn.transaction():
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vault_access_contributions (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                vault_id UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                -- 'direct', or an opaque '<namespace>:<id>' the grantor owns.
                -- AKB validates the shape and never interprets the value; the
                -- moment it does, it has imported the grantor's concept.
                source_key TEXT NOT NULL,
                granted_by UUID REFERENCES users(id),
                -- Monotonic per (vault, user, source). A retrying automated
                -- grantor carrying an older revision than the stored one is a
                -- no-op rather than an overwrite.
                revision BIGINT NOT NULL DEFAULT 1,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (vault_id, user_id, source_key),
                CONSTRAINT vault_access_contributions_role_check
                    CHECK (role IN ('reader', 'writer', 'admin')),
                CONSTRAINT vault_access_contributions_source_key_check
                    CHECK (
                        length(source_key) BETWEEN 1 AND 255
                        AND source_key !~ '[[:space:]]'
                    )
            )
            """
        )

        backfilled = await conn.fetchval(
            """
            WITH inserted AS (
                INSERT INTO vault_access_contributions
                    (vault_id, user_id, role, source_key, granted_by,
                     revision, created_at, updated_at)
                SELECT va.vault_id, va.user_id, va.role, 'direct', va.granted_by,
                       1, va.created_at, va.created_at
                FROM vault_access va
                WHERE va.role IN ('reader', 'writer', 'admin')
                ON CONFLICT (vault_id, user_id, source_key) DO NOTHING
                RETURNING 1
            )
            SELECT count(*) FROM inserted
            """
        )

        # A row whose role is outside the contribution vocabulary cannot be
        # represented, so it is reported rather than dropped or coerced: an
        # 'owner' row in vault_access would mean ownership had leaked into the
        # member plane, which is a different defect than this migration's.
        unrepresentable = await conn.fetchval(
            "SELECT count(*) FROM vault_access WHERE role NOT IN ('reader', 'writer', 'admin')"
        )

        # The claim this migration rests on: derived == stored, everywhere.
        divergent = await conn.fetchval(
            """
            SELECT count(*)
            FROM vault_access va
            LEFT JOIN LATERAL (
                SELECT c.role
                FROM vault_access_contributions c
                WHERE c.vault_id = va.vault_id AND c.user_id = va.user_id
                ORDER BY CASE c.role
                             WHEN 'admin' THEN 3 WHEN 'writer' THEN 2
                             WHEN 'reader' THEN 1 ELSE 0 END DESC
                LIMIT 1
            ) derived ON TRUE
            WHERE va.role IN ('reader', 'writer', 'admin')
              AND derived.role IS DISTINCT FROM va.role
            """
        )
        if divergent:
            raise RuntimeError(
                f"vault_access_contributions backfill left {divergent} pair(s) whose "
                "derived effective role differs from the stored one; refusing to "
                "commit a migration that moves access"
            )

    logger.info(
        "vault_access_contributions: backfilled %d direct contribution(s); "
        "derived effective role matches the stored one for every pair",
        backfilled or 0,
    )
    if unrepresentable:
        logger.warning(
            "%d vault_access row(s) carry a role outside (reader, writer, admin) and "
            "have no contribution; ownership belongs on vaults.owner_id, not here",
            unrepresentable,
        )
