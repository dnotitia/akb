"""Migration 098: preserve UTF-8 text payloads containing NUL bytes."""

from __future__ import annotations

import logging

logger = logging.getLogger("akb.migration.098")


async def migrate(conn=None):
    if conn is None:
        from app.db.postgres import get_pool

        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    if await conn.fetchval("SELECT to_regclass('public.m1_reference_payloads')") is None:
        logger.info("Migration 098 skipped: Native reference payloads are absent")
        return

    async with conn.transaction():
        await conn.execute(
            """
            CREATE OR REPLACE FUNCTION akb_is_utf8_payload(payload BYTEA)
            RETURNS BOOLEAN
            LANGUAGE plpgsql
            IMMUTABLE
            STRICT
            PARALLEL SAFE
            AS $$
            DECLARE
                offset_bytes INTEGER := 1;
                relative_nul INTEGER;
                payload_bytes INTEGER := octet_length(payload);
            BEGIN
                WHILE offset_bytes <= payload_bytes LOOP
                    relative_nul := position(
                        decode('00', 'hex')
                        IN substring(payload FROM offset_bytes)
                    );
                    IF relative_nul = 0 THEN
                        PERFORM convert_from(substring(payload FROM offset_bytes), 'UTF8');
                        EXIT;
                    END IF;
                    IF relative_nul > 1 THEN
                        PERFORM convert_from(
                            substring(payload FROM offset_bytes FOR relative_nul - 1),
                            'UTF8'
                        );
                    END IF;
                    offset_bytes := offset_bytes + relative_nul;
                END LOOP;
                RETURN TRUE;
            EXCEPTION
                WHEN SQLSTATE '22021' THEN
                    RETURN FALSE;
            END;
            $$;

            ALTER TABLE m1_reference_payloads
                DROP CONSTRAINT IF EXISTS m1_reference_payloads_text_check;
            ALTER TABLE m1_reference_payloads
                ADD CONSTRAINT m1_reference_payloads_text_check
                CHECK (
                    encoding = 'utf-8'
                    AND akb_is_utf8_payload(canonical_bytes)
                ) NOT VALID;
            ALTER TABLE m1_reference_payloads
                VALIDATE CONSTRAINT m1_reference_payloads_text_check;
            """
        )

    logger.info("Migration 098 enabled NUL-compatible UTF-8 payload validation")
