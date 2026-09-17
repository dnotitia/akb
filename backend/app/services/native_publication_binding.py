"""Transfer existing publication identity only through committed cutover evidence."""

from __future__ import annotations


async def rebind_verified_native_publications(conn) -> int:
    """Rebind mapped live Documents on the caller's authority transaction.

    Old isolated migration fixtures lack publication identity columns. Their
    absence is explicit; a database with the complete schema must execute the
    binding query and propagate errors. Merely sharing a UUID or path is never
    migration evidence. Current Heads may legitimately have advanced since
    cutover, so only the immutable Resource identity is transferred.
    """
    ready = await conn.fetchval("""
        SELECT EXISTS (
            SELECT 1 FROM pg_attribute
             WHERE attrelid = to_regclass('publications')
               AND attname = 'native_document_id' AND NOT attisdropped
        ) AND to_regclass('native_revision_existing_authority') IS NOT NULL
          AND to_regclass('native_revision_cutover_runs') IS NOT NULL
          AND to_regclass('native_revision_cutover_vaults') IS NOT NULL
          AND to_regclass('native_revision_migration_runs') IS NOT NULL
          AND to_regclass('native_revision_migration_items') IS NOT NULL
          AND to_regclass('native_resources') IS NOT NULL
    """)
    if not ready:
        return 0
    result = await conn.execute("""
        WITH eligible AS MATERIALIZED (
            SELECT p.slug, p.vault_id, p.document_id AS legacy_document_id, r.resource_id,
                   CASE WHEN strpos(r.current_path, '/') > 0
                     THEN 'akb://' || v.name || '/coll/'
                          || regexp_replace(r.current_path, '/[^/]*$', '')
                          || '/doc/' || regexp_replace(r.current_path, '^.*/', '')
                     ELSE 'akb://' || v.name || '/doc/' || r.current_path
                   END AS resource_uri
              FROM publications p
              JOIN native_resources r ON r.resource_id = p.document_id
                                     AND r.namespace_id = p.vault_id
                                     AND r.surface = 'document' AND r.lifecycle = 'live'
              JOIN vaults v ON v.id = r.namespace_id
             WHERE p.resource_type = 'document' AND p.native_document_id IS NULL
               AND EXISTS (
                   SELECT 1
                     FROM native_revision_existing_authority a
                     JOIN native_revision_cutover_runs c ON c.cutover_id = a.cutover_id
                     JOIN native_revision_cutover_vaults cv ON cv.cutover_id = c.cutover_id
                     JOIN native_revision_migration_runs mr ON mr.run_id = cv.migration_run_id
                                                          AND mr.namespace_id = cv.namespace_id
                     JOIN native_revision_migration_items mi ON mi.run_id = mr.run_id
                                                            AND mi.namespace_id = mr.namespace_id
                    WHERE a.status = 'committed' AND c.status = 'verified'
                      AND cv.status = 'verified' AND mr.status = 'complete' AND mi.status = 'complete'
                      AND cv.namespace_id = p.vault_id
                      AND mi.legacy_document_id = p.document_id
                      AND mi.native_resource_id = r.resource_id
               )
             FOR UPDATE OF r
        )
        UPDATE publications p
           SET document_id = NULL, native_document_id = eligible.resource_id,
               resource_uri = eligible.resource_uri
          FROM eligible
         WHERE p.slug = eligible.slug AND p.native_document_id IS NULL
           AND p.vault_id = eligible.vault_id AND p.document_id = eligible.legacy_document_id
    """)
    return int(result.rsplit(' ', 1)[-1])
