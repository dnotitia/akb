"""Cutover publication transfer requires evidence, never UUID/path coincidence."""
from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock

import asyncpg
import pytest

from app.services.native_publication_binding import rebind_verified_native_publications
from app.services.uri_service import doc_uri


@pytest.mark.asyncio
async def test_missing_publication_column_skips_narrow_fixture():
    conn = AsyncMock()
    conn.fetchval.return_value = False
    assert await rebind_verified_native_publications(conn) == 0
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_verified_binding_uses_identity_and_current_path_after_head_advances():
    dsn = os.environ.get('AKB_TEST_DSN')
    if not dsn:
        pytest.skip('AKB_TEST_DSN required for isolated PostgreSQL verification')
    admin = await asyncpg.connect(dsn)
    database = "native_publication_binding_" + uuid.uuid4().hex[:12]
    await admin.execute(f'CREATE DATABASE "{database}"')
    conn = None
    try:
        conn = await asyncpg.connect(dsn.rsplit('/', 1)[0] + '/' + database)
        # Connection-private tables also make to_regclass/pg_attribute exercise
        # the same search-path rules as isolated migration schemas.
        await conn.execute('''
            CREATE TEMP TABLE vaults (id uuid PRIMARY KEY, name text);
            CREATE TEMP TABLE publications (
                slug text PRIMARY KEY, vault_id uuid, resource_type text,
                document_id uuid, native_document_id uuid, resource_uri text
            );
            CREATE TEMP TABLE native_resources (
                resource_id uuid PRIMARY KEY, namespace_id uuid, surface text,
                lifecycle text, current_path text, head_revision_id text
            );
            CREATE TEMP TABLE native_revision_existing_authority (cutover_id uuid, status text);
            CREATE TEMP TABLE native_revision_cutover_runs (cutover_id uuid, status text);
            CREATE TEMP TABLE native_revision_cutover_vaults (
                cutover_id uuid, namespace_id uuid, migration_run_id uuid, status text
            );
            CREATE TEMP TABLE native_revision_migration_runs (run_id uuid, namespace_id uuid, status text);
            CREATE TEMP TABLE native_revision_migration_items (
                run_id uuid, namespace_id uuid, legacy_document_id uuid,
                native_resource_id uuid, native_head_revision_id text, status text
            );
        ''')
        namespace, cutover, run = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        await conn.execute("INSERT INTO vaults VALUES ($1, 'bound-vault')", namespace)
        await conn.execute("INSERT INTO native_revision_existing_authority VALUES ($1,'committed')", cutover)
        await conn.execute("INSERT INTO native_revision_cutover_runs VALUES ($1,'verified')", cutover)
        await conn.execute("INSERT INTO native_revision_cutover_vaults VALUES ($1,$2,$3,'verified')", cutover, namespace, run)
        await conn.execute("INSERT INTO native_revision_migration_runs VALUES ($1,$2,'complete')", run, namespace)
        cases = {
            'mapped': ('new/nested/moved.md', 'live', 'document', 'complete'),
            'root': ('root.md', 'live', 'document', 'complete'),
            'unmapped': ('old.md', 'live', 'document', None),
            'incomplete': ('old.md', 'live', 'document', 'pending'),
            'deleted': ('old.md', 'deleted', 'document', 'complete'),
            'file': ('old.md', 'live', 'file', 'complete'),
        }
        ids = {}
        for slug, (path, lifecycle, surface, status) in cases.items():
            identity = ids[slug] = uuid.uuid4()
            await conn.execute("INSERT INTO publications VALUES ($1,$2,'document',$3,NULL,'akb://bound-vault/doc/old.md')",
                               slug, namespace, identity)
            await conn.execute("INSERT INTO native_resources VALUES ($1,$2,$3,$4,$5,'new-head')",
                               identity, namespace, surface, lifecycle, path)
            if status:
                await conn.execute("INSERT INTO native_revision_migration_items VALUES ($1,$2,$3,$3,'initial-head',$4)",
                                   run, namespace, identity, status)
        assert await rebind_verified_native_publications(conn) == 2
        rows = {row['slug']: row for row in await conn.fetch('SELECT * FROM publications')}
        for slug in ('mapped', 'root'):
            assert rows[slug]['native_document_id'] == ids[slug]
            assert rows[slug]['document_id'] is None
            assert rows[slug]['resource_uri'] == doc_uri('bound-vault', cases[slug][0])
        for slug in ('unmapped', 'incomplete', 'deleted', 'file'):
            assert rows[slug]['native_document_id'] is None
            assert rows[slug]['document_id'] == ids[slug]
        assert await rebind_verified_native_publications(conn) == 0
        # A migration item alone is insufficient when its authority or vault
        # receipt is no longer verified/committed.
        await conn.execute("UPDATE native_revision_migration_items SET status='complete' WHERE legacy_document_id=$1", ids['incomplete'])
        await conn.execute("UPDATE native_revision_existing_authority SET status='pending'")
        assert await rebind_verified_native_publications(conn) == 0
        await conn.execute("UPDATE native_revision_existing_authority SET status='committed'")
        await conn.execute("UPDATE native_revision_cutover_vaults SET status='applied'")
        assert await rebind_verified_native_publications(conn) == 0
    finally:
        if conn is not None:
            await conn.close()
        await admin.execute(f'DROP DATABASE "{database}" WITH (FORCE)')
        await admin.close()
