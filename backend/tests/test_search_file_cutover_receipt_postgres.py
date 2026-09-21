"""Execute the hydration receipt predicate against isolated temporary PG rows."""
import os

import asyncpg
import pytest

from app.services.search_service import _VERIFIED_FILE_CUTOVER_PATH_SQL


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    None,
    "UPDATE native_revision_cutover_runs SET status = 'pending'",
    "UPDATE native_revision_cutover_files SET status = 'pending'",
    "UPDATE native_revision_cutover_files SET disposition = 'preserved_binary'",
    "UPDATE native_revision_cutover_files SET file_id = 'other'",
    "UPDATE native_revision_cutover_files SET namespace_id = 'other'",
    "UPDATE f SET name = 'renamed.txt'",
    "UPDATE f SET mime_type = 'text/markdown'",
    "UPDATE f SET content_hash = 'new-content'",
    "UPDATE f SET size_bytes = 12",
    "UPDATE f SET s3_key = 'new-object'",
    "UPDATE f SET etag = 'new-etag'",
    "UPDATE f SET storage_version = 'new-version'",
    "UPDATE f SET hash_verified_at = NULL",
    "UPDATE r SET current_path = 'other-path'",
    "UPDATE r SET head_revision_id = 'new-head'",
    "UPDATE p SET digest = 'wrong-body'",
    "UPDATE p SET byte_size = 12",
    "INSERT INTO native_file_projection_outbox VALUES ('file')",
])
async def test_verified_collision_receipt_requires_exact_current_source_and_head(mutation):
    dsn = os.environ.get("AKB_TEST_DSN")
    if not dsn:
        pytest.skip("AKB_TEST_DSN required for PostgreSQL receipt verification")
    conn = await asyncpg.connect(dsn)
    try:
        # All names are session-local temporary tables, even when a caller
        # supplies a database with existing product tables of the same names.
        await conn.execute("""
            CREATE TEMP TABLE f (id text, vault_id text, name text, mime_type text,
                content_hash text, size_bytes bigint, s3_key text, etag text,
                storage_version text, hash_verified_at timestamptz);
            INSERT INTO f VALUES ('file', 'vault', 'same.txt', 'text/plain',
                'hash', 4, 'key', NULL, NULL, NOW());
            CREATE TEMP TABLE col (path text);
            INSERT INTO col VALUES ('files');
            CREATE TEMP TABLE r (current_path text, head_revision_id text);
            INSERT INTO r VALUES ('files/same--collision.txt', 'head');
            CREATE TEMP TABLE p (digest text, byte_size bigint);
            INSERT INTO p VALUES ('hash', 4);
            CREATE TEMP TABLE native_revision_cutover_runs (cutover_id text, status text);
            INSERT INTO native_revision_cutover_runs VALUES ('run', 'verified');
            CREATE TEMP TABLE native_revision_cutover_files (
                cutover_id text, file_id text, namespace_id text, status text,
                disposition text, logical_path text, applied_path text,
                native_revision_id text, mime_type text, content_hash text,
                byte_size bigint, s3_key text, etag text, storage_version text);
            INSERT INTO native_revision_cutover_files VALUES ('run', 'file', 'vault',
                'verified', 'native_text', 'files/same.txt', 'files/same--collision.txt',
                'head', 'text/plain', 'hash', 4, 'key', NULL, NULL);
            CREATE TEMP TABLE native_file_projection_outbox (file_id text);
        """)
        if mutation:
            await conn.execute(mutation)
        actual = await conn.fetchval(
            f"SELECT {_VERIFIED_FILE_CUTOVER_PATH_SQL} FROM f CROSS JOIN col CROSS JOIN r CROSS JOIN p"
        )
        assert actual is (mutation is None)
    finally:
        await conn.close()
