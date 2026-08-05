"""Migration 053: add the explicit M1 PostgreSQL BodyStore candidate.

Migration 048 deliberately labelled its BYTEA payload as a reference adapter so
the B-core experiment could not accidentally select a physical text profile.
M1's B-text arm needs a separately labelled candidate while retaining the same
manifest foreign-key, integrity, and content-addressed deduplication checks.
The experiment keeps one placement profile per namespace; the existing
``(namespace_id, digest, byte_size)`` key and an advisory-lock-backed insert
trigger reject mixed-profile coexistence across the whole namespace rather
than creating ambiguous manifest identity.
It admits ``pg-bodystore-v1`` without changing any legacy/default write path.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("akb.migration.053")


async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool

        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    async with conn.transaction():
        await conn.execute(
            """
            ALTER TABLE m1_reference_payloads
                DROP CONSTRAINT IF EXISTS m1_reference_payloads_placement_check;
            ALTER TABLE m1_reference_payloads
                ADD CONSTRAINT m1_reference_payloads_placement_check
                CHECK (selected_placement IN (
                    'm1-reference-payload-v1',
                    'pg-bodystore-v1'
                ));

            CREATE OR REPLACE FUNCTION akb_m1_enforce_namespace_payload_placement()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                PERFORM pg_advisory_xact_lock(
                    hashtextextended(NEW.namespace_id::text, 917049)
                );
                IF EXISTS (
                    SELECT 1
                      FROM m1_reference_payloads existing
                     WHERE existing.namespace_id = NEW.namespace_id
                       AND existing.selected_placement <> NEW.selected_placement
                ) THEN
                    RAISE EXCEPTION
                        'M1 measurement namespace cannot mix payload placements'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$;

            DROP TRIGGER IF EXISTS trg_m1_namespace_payload_placement
                ON m1_reference_payloads;
            CREATE TRIGGER trg_m1_namespace_payload_placement
                BEFORE INSERT ON m1_reference_payloads
                FOR EACH ROW
                EXECUTE FUNCTION akb_m1_enforce_namespace_payload_placement();
            """
        )
    logger.info("Migration 053: explicit M1 PostgreSQL BodyStore candidate ready")
