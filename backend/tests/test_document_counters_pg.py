"""Every document counter follows the active authority (akb#525).

`test_vault_info_counters_pg.py` pins `GET /vaults/{vault}/info`. This file
pins the counter surfaces that endpoint does NOT cover, and the half of the
property the issue does not state: that an UPDATE through the ordinary write
path moves the numbers, not only a create.

Two of the three surfaces here never read `documents` at all, which is why
grepping for the query in the issue does not find them:

* `akb_browse` reports each collection's `doc_count` / `last_updated` from
  the denormalised `collections` columns, which only the LEGACY write path
  bumps (`DocumentRepository.increment_count`). On a native installation the
  browse payload therefore shows a frozen count directly above the documents
  that contradict it.
* The operator corpus inventory (`app/stats/sampler`) counts the legacy
  catalog installation-wide, so it reports the pre-cutover total forever.

The writes go through `NativeRevisionService.create_text` / `replace_text`,
the real native write primitives, rather than hand-built rows: the
last-activity query relies on `set_head` keeping
`native_resources.updated_at` equal to the head revision's `occurred_at`, and
a fixture that stamped that column itself would be asserting its own
arithmetic. Real Postgres, fresh database per test, self-skips if
unreachable — the same convention as the neighbouring `*_pg.py` files.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest

from app.services import access_service
from app.services.native_document_service import NativeDocumentService
from app.services.native_revision_service import NativeRevisionService


pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text(encoding="utf-8")
_MIGRATIONS = _BACKEND / "app" / "db" / "migrations"
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:5433/akb",  # pragma: allowlist secret
)

# A believable, motionless number: what a cutover leaves behind in the
# denormalised columns, and what every assertion below must refuse to echo.
_FROZEN_COUNT = 41
_FROZEN_AT = datetime(2026, 5, 14, 9, 44, 12, tzinfo=timezone.utc)


async def _reachable() -> bool:
    try:
        conn = await asyncpg.connect(_DSN, timeout=2)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


def _database_dsn(name: str) -> str:
    return f"{_DSN.rsplit('/', 1)[0]}/{name}"


def _load(filename: str):
    path = _MIGRATIONS / filename
    spec = importlib.util.spec_from_file_location(f"counters_migration_{filename}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@asynccontextmanager
async def _fresh_database(monkeypatch):
    """A vault with an owner, on a database carrying the native core."""
    if not await _reachable():
        pytest.skip(f"Postgres not reachable at {_DSN}")

    name = f"akb_document_counters_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(_DSN)
    conn = None
    pool = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        conn = await asyncpg.connect(_database_dsn(name))
        await conn.execute(_INIT_SQL)
        for filename in (
            "010_external_git_mirror.py",
            "015_events_outbox.py",
            "044_vault_write_policy.py",
            "045_vault_write_grant_actions.py",
            "048_native_revision_core.py",
        ):
            await _load(filename).migrate(conn=conn)
        await conn.close()
        conn = None
        pool = await asyncpg.create_pool(_database_dsn(name), min_size=1, max_size=4)

        suffix = uuid.uuid4().hex[:12]
        vault_name = f"dc-vault-{suffix}"
        async with pool.acquire() as seeded:
            owner_id = await seeded.fetchval(
                "INSERT INTO users (username, email, password_hash) "
                "VALUES ($1, $2, 'x') RETURNING id",
                f"dc-{suffix}",
                f"dc-{suffix}@example.com",
            )
            vault_id = await seeded.fetchval(
                "INSERT INTO vaults (name, git_path, owner_id) VALUES ($1, $2, $3) RETURNING id",
                vault_name,
                f"/tmp/dc-{suffix}-unused.git",
                owner_id,
            )

        from app.repositories import vault_write_policy_repo
        from app.services import auth_service, table_service

        async def _get_pool():
            return pool

        monkeypatch.setattr(auth_service, "get_pool", _get_pool)
        monkeypatch.setattr(vault_write_policy_repo, "get_pool", _get_pool)
        monkeypatch.setattr(access_service, "get_pool", _get_pool)
        monkeypatch.setattr(table_service, "get_pool", _get_pool)

        yield pool, vault_id, vault_name, owner_id
    finally:
        if pool is not None:
            await pool.close()
        if conn is not None and not conn.is_closed():
            await conn.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


def _native_backend(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "document_revision_backend", "postgres_native", raising=False)
    monkeypatch.setattr(settings, "native_revision_m1_measurement_only", False, raising=False)
    monkeypatch.setattr(settings, "db_name", "akb", raising=False)


def _legacy_backend(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "document_revision_backend", "bare_git", raising=False)


async def _write_native_doc(pool, vault_id, path: str, *, actor: str, body: str):
    return await NativeRevisionService(pool).create_text(
        namespace_id=vault_id,
        surface="document",
        path=path,
        payload=body,
        actor=actor,
        mutation_id=uuid.uuid4(),
        resource_id=uuid.uuid4(),
    )


async def _freeze_collection(pool, vault_id, path: str, name: str) -> None:
    """A collection row as the cutover leaves it: real row, stale counters."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO collections (vault_id, path, name, doc_count, last_updated) "
            "VALUES ($1, $2, $3, $4, $5)",
            vault_id,
            path,
            name,
            _FROZEN_COUNT,
            _FROZEN_AT,
        )


async def _freeze_legacy_document(pool, vault_id, path: str) -> None:
    """One retained pre-cutover catalog row, the kind the projection kept."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO documents (vault_id, path, title, content_hash, updated_at, created_by) "
            "VALUES ($1, $2, $3, 'x', $4, 'pre-cutover-author')",
            vault_id,
            path,
            path.rsplit("/", 1)[-1],
            _FROZEN_AT,
        )


# ── akb_browse ───────────────────────────────────────────────


async def test_browse_collection_counters_read_the_native_authority(monkeypatch):
    """A collection lists the documents it really holds, and when they landed.

    The frozen `collections.doc_count` is the number a cutover leaves behind;
    browse must report 2 and a timestamp from today, next to the two document
    items in the very same payload.
    """
    _native_backend(monkeypatch)
    async with _fresh_database(monkeypatch) as (pool, vault_id, vault_name, _owner):
        await _freeze_collection(pool, vault_id, "notes", "notes")
        await _write_native_doc(pool, vault_id, "notes/a.md", actor="writer", body="alpha\n")
        await _write_native_doc(pool, vault_id, "notes/b.md", actor="writer", body="beta\n")

        response = await NativeDocumentService(pool=pool).browse(
            vault_name, content_type="documents",
        )

    collections = [i for i in response.items if i.type == "collection"]
    documents = [i for i in response.items if i.type == "document"]
    assert len(collections) == 1
    assert len(documents) == 2, "the documents the count is meant to describe"
    assert collections[0].doc_count == 2
    assert collections[0].last_updated is not None
    assert collections[0].last_updated > _FROZEN_AT


async def test_browse_collection_with_no_live_documents_reads_zero(monkeypatch):
    """An emptied collection reads 0, never the frozen number.

    This is the shape the issue calls dangerous in reverse: a stale non-zero
    beside nothing is as wrong as a stale zero beside documents, and only one
    of the two can be caught by looking for a suspicious zero.
    """
    _native_backend(monkeypatch)
    async with _fresh_database(monkeypatch) as (pool, vault_id, vault_name, _owner):
        await _freeze_collection(pool, vault_id, "emptied", "emptied")

        response = await NativeDocumentService(pool=pool).browse(
            vault_name, content_type="documents",
        )

    collections = [i for i in response.items if i.type == "collection"]
    assert len(collections) == 1
    assert collections[0].doc_count == 0
    assert collections[0].last_updated is None


async def test_browse_counts_direct_children_not_the_subtree(monkeypatch):
    """`doc_count` keeps its legacy meaning: documents directly inside.

    `increment_count` is called with the document's own `collection_id`, so a
    nested document belongs to the nested collection. Recounting from the
    native ledger must not quietly turn this into a subtree total.
    """
    _native_backend(monkeypatch)
    async with _fresh_database(monkeypatch) as (pool, vault_id, vault_name, _owner):
        await _freeze_collection(pool, vault_id, "outer", "outer")
        await _freeze_collection(pool, vault_id, "outer/inner", "inner")
        await _write_native_doc(pool, vault_id, "outer/top.md", actor="writer", body="top\n")
        await _write_native_doc(pool, vault_id, "outer/inner/deep.md", actor="writer", body="deep\n")

        response = await NativeDocumentService(pool=pool).browse(
            vault_name, content_type="documents", depth=-1,
        )

    counts = {i.path: i.doc_count for i in response.items if i.type == "collection"}
    assert counts == {"outer": 1, "outer/inner": 1}


async def test_browse_into_a_subtree_counts_that_subtree(monkeypatch):
    """Browsing into a collection scopes the scan and still counts right.

    The scan is narrowed to the prefix because only collections under it are
    rendered. A narrowing that dropped a nested collection, or that matched
    a sibling because `_` is a LIKE wildcard, would show up here.
    """
    _native_backend(monkeypatch)
    async with _fresh_database(monkeypatch) as (pool, vault_id, vault_name, _owner):
        await _freeze_collection(pool, vault_id, "team_notes", "team_notes")
        await _freeze_collection(pool, vault_id, "team_notes/drafts", "drafts")
        await _freeze_collection(pool, vault_id, "teamXnotes", "teamXnotes")
        await _write_native_doc(pool, vault_id, "team_notes/drafts/a.md", actor="w", body="a\n")
        await _write_native_doc(pool, vault_id, "team_notes/drafts/b.md", actor="w", body="b\n")
        await _write_native_doc(pool, vault_id, "teamXnotes/sibling.md", actor="w", body="s\n")

        response = await NativeDocumentService(pool=pool).browse(
            vault_name, collection="team_notes", content_type="documents", depth=-1,
        )

    counts = {i.path: i.doc_count for i in response.items if i.type == "collection"}
    assert counts == {"team_notes/drafts": 2}


async def test_browse_on_bare_git_keeps_the_stored_columns(monkeypatch):
    """The legacy arm is untouched: the stored counters are its authority,
    and a native resource must not leak into them."""
    _legacy_backend(monkeypatch)
    async with _fresh_database(monkeypatch) as (pool, vault_id, vault_name, _owner):
        await _freeze_collection(pool, vault_id, "notes", "notes")
        await _write_native_doc(pool, vault_id, "notes/a.md", actor="writer", body="alpha\n")

        response = await NativeDocumentService(pool=pool).browse(
            vault_name, content_type="documents",
        )

    collections = [i for i in response.items if i.type == "collection"]
    assert collections[0].doc_count == _FROZEN_COUNT
    assert collections[0].last_updated == _FROZEN_AT


# ── vault info: updates, not only creates ────────────────────


async def test_vault_last_activity_follows_an_update(monkeypatch):
    """`last_activity` moves when a document is EDITED, not only created.

    Measured on one installation before this arc: three documents repaired
    through the ordinary update route carried native revisions stamped that
    day and `documents.updated_at` values four months old. The vault's
    reported last activity was the stale one. Nothing in the retained catalog
    row moves, so the assertion is that the reported time comes from the
    edit and the reported user is whoever made it.
    """
    _native_backend(monkeypatch)
    async with _fresh_database(monkeypatch) as (pool, vault_id, vault_name, owner_id):
        await _freeze_legacy_document(pool, vault_id, "notes/a.md")
        created = await _write_native_doc(
            pool, vault_id, "notes/a.md", actor="original-author", body="first\n",
        )
        await NativeRevisionService(pool).replace_text(
            namespace_id=vault_id,
            surface="document",
            path="notes/a.md",
            payload="second\n",
            actor="repairing-editor",
            mutation_id=uuid.uuid4(),
            expected_revision_id=created.revision_id,
        )

        info = await access_service.get_vault_info(str(owner_id), vault_name)

        async with pool.acquire() as conn:
            head_at = await conn.fetchval(
                "SELECT nr.occurred_at FROM native_resources r "
                "JOIN native_revisions nr ON nr.revision_id = r.head_revision_id "
                "WHERE r.namespace_id = $1 AND r.current_path = 'notes/a.md'",
                vault_id,
            )

    assert info["document_count"] == 1
    assert info["last_active_user"] == "repairing-editor"
    assert info["last_activity"] == head_at.isoformat()
    assert head_at > _FROZEN_AT + timedelta(days=1), "not the retained catalog timestamp"


async def test_vault_last_activity_picks_the_newest_of_several_documents(monkeypatch):
    """Ordering runs over resources; the actor must come from the same row.

    The reported timestamp and the reported user are read from two different
    tables, so a form that ordered one and joined the other loosely could
    return a time from one document and a name from another.
    """
    _native_backend(monkeypatch)
    async with _fresh_database(monkeypatch) as (pool, vault_id, vault_name, owner_id):
        await _write_native_doc(pool, vault_id, "notes/old.md", actor="earlier", body="old\n")
        await _write_native_doc(pool, vault_id, "notes/new.md", actor="later", body="new\n")

        info = await access_service.get_vault_info(str(owner_id), vault_name)

        async with pool.acquire() as conn:
            newest = await conn.fetchval(
                "SELECT r.updated_at FROM native_resources r "
                "WHERE r.namespace_id = $1 AND r.current_path = 'notes/new.md'",
                vault_id,
            )

    assert info["last_active_user"] == "later"
    assert info["last_activity"] == newest.isoformat()


# ── operator corpus inventory ────────────────────────────────


async def test_instance_corpus_count_reads_the_native_authority(monkeypatch):
    """The installation-wide document total counts what exists now.

    The retained catalog row is the pre-cutover population; the native
    resources are the real one. Counting the catalog reports a total that
    stopped moving at the cutover.
    """
    _native_backend(monkeypatch)
    from app.stats import sampler

    async with _fresh_database(monkeypatch) as (pool, vault_id, _vault_name, _owner):
        await _freeze_legacy_document(pool, vault_id, "notes/retained.md")
        await _write_native_doc(pool, vault_id, "notes/a.md", actor="writer", body="alpha\n")
        await _write_native_doc(pool, vault_id, "notes/b.md", actor="writer", body="beta\n")
        await _write_native_doc(pool, vault_id, "notes/c.md", actor="writer", body="gamma\n")

        async with pool.acquire() as conn:
            corpus = await sampler._read_corpus(conn)

    assert corpus["doc_count"] == 3


async def test_instance_corpus_count_on_bare_git_reads_the_catalog(monkeypatch):
    """The legacy arm still counts `documents`, with no native leak."""
    _legacy_backend(monkeypatch)
    from app.stats import sampler

    async with _fresh_database(monkeypatch) as (pool, vault_id, _vault_name, _owner):
        await _freeze_legacy_document(pool, vault_id, "notes/retained.md")
        await _write_native_doc(pool, vault_id, "notes/a.md", actor="writer", body="alpha\n")

        async with pool.acquire() as conn:
            corpus = await sampler._read_corpus(conn)

    assert corpus["doc_count"] == 1
