"""Native explicit-source resolution must not scan the document corpus.

Uses a disposable database on AKB_SEARCH_TEST_DSN; no shared tables change.
"""
from __future__ import annotations

import json
import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from app.services.search_service import SearchService

pytestmark = pytest.mark.asyncio
_USER = uuid.UUID(int=100_001)


@pytest.fixture
async def scope_conn():
    dsn = os.environ.get("AKB_SEARCH_TEST_DSN")
    if not dsn:
        pytest.skip("AKB_SEARCH_TEST_DSN is required")
    name = f"akb_native_scope_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(dsn)
    conn = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        conn = await asyncpg.connect(urlunsplit(urlsplit(dsn)._replace(path=f"/{name}")))
        await conn.execute("""
            CREATE TABLE vaults (
                id uuid PRIMARY KEY, name text UNIQUE, owner_id uuid, public_access text
            );
            CREATE TABLE vault_access (vault_id uuid, user_id uuid);
            CREATE TABLE native_resources (
                resource_id uuid PRIMARY KEY, namespace_id uuid, surface text,
                current_path text, lifecycle text, head_revision_id text
            );
            CREATE UNIQUE INDEX uq_native_resources_live_path
                ON native_resources(namespace_id, current_path) WHERE lifecycle='live';
            CREATE TABLE native_revisions (
                revision_id text PRIMARY KEY, resource_id uuid, payload_manifest_id uuid
            );
            CREATE TABLE native_payload_manifests (
                payload_manifest_id uuid PRIMARY KEY, private_locator uuid
            );
            CREATE TABLE m1_reference_payloads (
                payload_id uuid PRIMARY KEY, byte_size bigint, digest text, encoding text,
                selected_placement text, verification_profile text, canonical_bytes bytea
            );
            INSERT INTO vaults VALUES
                ('00000000-0000-0000-0000-000000000001', 'a',
                 '00000000-0000-0000-0000-0000000186a1', NULL),
                ('00000000-0000-0000-0000-000000000002', 'b',
                 '00000000-0000-0000-0000-0000000186a2', NULL);
            INSERT INTO native_resources
            SELECT lpad(to_hex(n), 32, '0')::uuid,
                   lpad(to_hex(1 + n % 2), 32, '0')::uuid,
                   'document', 'doc-' || n || '.md', 'live', n::text
              FROM generate_series(1, 60000) n;
            INSERT INTO native_resources VALUES
                ('00000000-0000-0000-0000-00000000ea61',
                 '00000000-0000-0000-0000-000000000001', 'document', 'same.md', 'live', '60001'),
                ('00000000-0000-0000-0000-00000000ea62',
                 '00000000-0000-0000-0000-000000000002', 'document', 'same.md', 'live', '60002'),
                ('00000000-0000-0000-0000-00000000ea63',
                 '00000000-0000-0000-0000-000000000001', 'document',
                 '00000000-0000-0000-0000-00000000ea61', 'live', '60003'),
                ('00000000-0000-0000-0000-00000000ea64',
                 '00000000-0000-0000-0000-000000000001', 'document', 'archived.md', 'live', '60004'),
                ('00000000-0000-0000-0000-00000000ea65',
                 '00000000-0000-0000-0000-000000000001', 'document', 'deleted.md', 'deleted', '60005'),
                ('00000000-0000-0000-0000-00000000ea66',
                 '00000000-0000-0000-0000-000000000001', 'file', 'file.md', 'live', '60006');
            INSERT INTO native_revisions
                SELECT head_revision_id, resource_id, resource_id FROM native_resources;
            INSERT INTO native_payload_manifests
                SELECT resource_id, resource_id FROM native_resources;
            INSERT INTO m1_reference_payloads
                SELECT resource_id, 48, repeat('0',64), 'utf-8', 'reference', 'test',
                       convert_to(CASE WHEN current_path='archived.md'
                           THEN E'---\nstatus: archived\n---\narchived body\n'
                           ELSE E'---\nstatus: active\n---\nplain body\n' END, 'utf-8')
                  FROM native_resources;
            ANALYZE;
        """)
        yield conn
    finally:
        if conn is not None:
            await conn.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


class _ObservedConnection:
    def __init__(self, conn):
        self.conn = conn
        self.queries = []

    def __getattr__(self, name):
        return getattr(self.conn, name)

    async def fetchrow(self, sql, *params):
        self.queries.append((sql, params))
        return await self.conn.fetchrow(sql, *params)

    async def fetch(self, sql, *params):
        self.queries.append((sql, params))
        return await self.conn.fetch(sql, *params)


def _plan_nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from _plan_nodes(child)


async def _candidates(conn, uris, **overrides):
    args = dict(user_uuid=None, is_admin=True, vaults=None, collection=None,
                doc_type=None, tags=None, include_archived=False, source_uris=uris)
    args.update(overrides)
    return await SearchService()._native_document_candidates(conn, **args)


@pytest.mark.parametrize("scope_size", [1, 26])
async def test_explicit_sources_read_only_matching_native_resources(scope_conn, scope_size):
    observed = _ObservedConnection(scope_conn)
    numbers = list(range(60_000 - (scope_size - 1) * 2, 60_001, 2))
    uris = [f"akb://a/doc/doc-{number}.md" for number in numbers]
    # Run past asyncpg's prepared-statement custom-plan trials as well.
    for _ in range(8):
        candidates, _ = await _candidates(observed, uris)
        assert candidates == [str(uuid.UUID(int=number)) for number in numbers]
    for sql, params in observed.queries:
        plan = json.loads(await scope_conn.fetchval(
            "EXPLAIN (ANALYZE, FORMAT JSON) " + sql, *params,
        ))[0]["Plan"]
        scanned = sum(
            (node.get("Actual Rows", 0) + node.get("Rows Removed by Filter", 0))
            * node.get("Actual Loops", 0)
            for node in _plan_nodes(plan) if node.get("Relation Name") == "native_resources"
        )
        assert scanned < 100, f"Explicit {scope_size}-document scope read {scanned} resource rows"


async def test_native_source_scope_preserves_pairs_ids_acl_lifecycle_and_archive(scope_conn):
    uris = [
        "akb://a/doc/00000000-0000-0000-0000-00000000ea61",
        "akb://a/doc/same.md",  # duplicate identity must not duplicate a result
        "akb://b/doc/no-match.md",  # must not admit b/same.md by cross-pairing
        "akb://b/doc/doc-1.md",  # correct pair, but ACL forbids it
        "akb://a/doc/archived.md",
        "akb://a/doc/deleted.md",
        "akb://a/doc/file.md",  # incorrect surface
        "akb://a/table/same.md",
    ]
    candidates, _ = await _candidates(scope_conn, uris, user_uuid=_USER, is_admin=False)
    assert candidates == [str(uuid.UUID(int=60_001)), str(uuid.UUID(int=60_003))]
    archived, _ = await _candidates(
        scope_conn, uris, user_uuid=_USER, is_admin=False, archive_scope="archived",
    )
    assert archived == [str(uuid.UUID(int=60_004))]
    scoped, _ = await _candidates(scope_conn, uris, vaults=["b"])
    assert scoped == [str(uuid.UUID(int=1))]
