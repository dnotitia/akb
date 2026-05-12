"""Tests for CollectionService.create.

Mirrors the bootstrap pattern from `test_collection_repo.py`: hits a real
Postgres reachable via `AKB_TEST_DSN` (auto-skip otherwise), applies the
idempotent `init.sql`, and creates an ephemeral vault per test so the
table cascade cleans everything up.

`CollectionService` reaches into `app.db.postgres.get_pool()` for both
the repo wiring and the `emit_event` transaction. We monkeypatch that
function in the service module to hand back the test pool, so the
service code under test is exercised verbatim.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from app.repositories.vault_repo import VaultRepository

_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",
)


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2.0)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


@pytest_asyncio.fixture
async def pool():
    if not await _can_connect(_DSN):
        pytest.skip(f"Postgres not reachable at {_DSN}")
    pool = await asyncpg.create_pool(dsn=_DSN, min_size=1, max_size=4)
    backend_dir = Path(__file__).resolve().parents[1]
    init_sql = (backend_dir / "app" / "db" / "init.sql").read_text()
    async with pool.acquire() as conn:
        await conn.execute(init_sql)
    # `events` lives in migration 015 and `s3_delete_outbox` in
    # migration 019, neither in init.sql, but both are part of the
    # service contract (emit_event + cascade file delete) — apply them
    # so the tables exist. Migrations are idempotent.
    import importlib.util
    for mig_name in ("015_events_outbox.py", "019_s3_delete_outbox.py"):
        mig_path = backend_dir / "app" / "db" / "migrations" / mig_name
        spec = importlib.util.spec_from_file_location(mig_name, str(mig_path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        async with pool.acquire() as conn:
            await module.migrate(conn=conn)
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def vault_id(pool):
    vault_repo = VaultRepository(pool)
    name = f"_test_collection_service_{uuid.uuid4().hex[:8]}"
    vid = await vault_repo.create(
        name=name,
        description="ephemeral test vault",
        git_path=f"/tmp/{name}.git",
        owner_id=None,
    )
    try:
        yield vid
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vaults WHERE id = $1", vid)


@pytest_asyncio.fixture
async def vault_name(pool, vault_id):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
    return row["name"]


@pytest_asyncio.fixture
async def service(pool, monkeypatch):
    """Wire the service module's `get_pool` to the test pool.

    The service calls `get_pool()` twice — once for `_repos`, once for the
    transactional `emit_event` block. Returning the same test pool from
    both calls is enough to keep the service code under test unchanged.
    """
    from app.services import collection_service as cs

    async def _fake_get_pool():
        return pool

    monkeypatch.setattr(cs, "get_pool", _fake_get_pool)
    return cs.CollectionService()


@pytest.mark.asyncio
async def test_create_normalizes_and_returns_created_true(
    service, vault_name, pool, vault_id
):
    result = await service.create(
        vault=vault_name,
        path="  /specs/  ",
        summary="design specs",
        agent_id="alice",
    )
    assert result["ok"] is True
    assert result["created"] is True
    assert result["collection"]["path"] == "specs"
    assert result["collection"]["name"] == "specs"
    assert result["collection"]["summary"] == "design specs"
    assert result["collection"]["doc_count"] == 0

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT path, name, summary FROM collections WHERE vault_id=$1 AND path=$2",
            vault_id, "specs",
        )
    assert row is not None
    assert row["path"] == "specs"
    assert row["name"] == "specs"
    assert row["summary"] == "design specs"


@pytest.mark.asyncio
async def test_create_idempotent(service, vault_name):
    first = await service.create(
        vault=vault_name, path="docs/api", summary="v1", agent_id=None,
    )
    second = await service.create(
        vault=vault_name, path="docs/api", summary="ignored", agent_id=None,
    )
    assert first["created"] is True
    assert second["created"] is False
    # Path should round-trip identically on the no-op call too.
    assert second["collection"]["path"] == "docs/api"
    assert second["collection"]["name"] == "api"
    # Contract: an idempotent re-create reports stored state, not the
    # caller's args. The DB still has summary='v1' from the first call,
    # so the response must surface that — not "ignored".
    assert second["collection"]["summary"] == "v1"


@pytest.mark.asyncio
async def test_create_idempotent_reflects_real_doc_count(
    service, vault_name, pool, vault_id
):
    """A no-op re-create against a collection with N docs must report
    `doc_count == N`, not the hardcoded 0 the in-memory envelope used to
    show. Bumps the counter directly via the repository (no full
    document put pathway needed) so the test stays focused on the
    response-shape contract.
    """
    from datetime import datetime, timezone

    from app.repositories.document_repo import CollectionRepository

    first = await service.create(
        vault=vault_name, path="loaded", summary="seed", agent_id=None,
    )
    assert first["collection"]["doc_count"] == 0

    coll_repo = CollectionRepository(pool)
    # Look up the collection id, bump it twice to simulate two docs.
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM collections WHERE vault_id=$1 AND path=$2",
            vault_id, "loaded",
        )
    now = datetime.now(timezone.utc)
    await coll_repo.increment_count(row["id"], now)
    await coll_repo.increment_count(row["id"], now)

    second = await service.create(
        vault=vault_name, path="loaded", summary=None, agent_id=None,
    )
    assert second["created"] is False
    assert second["collection"]["doc_count"] == 2
    assert second["collection"]["summary"] == "seed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    ["", "   ", "/", "../etc", "a/../b", "a/./b", "x\x00y"],
)
async def test_create_rejects_invalid_path(service, vault_name, bad):
    from app.services.collection_service import InvalidPathError

    with pytest.raises(InvalidPathError):
        await service.create(
            vault=vault_name, path=bad, summary=None, agent_id=None,
        )


@pytest.mark.asyncio
async def test_create_emits_event(service, vault_name, pool, vault_id):
    await service.create(
        vault=vault_name, path="events/probe", summary=None, agent_id="bob",
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT kind, ref_type, ref_id, actor_id, payload
              FROM events
             WHERE vault_id = $1 AND kind = 'collection.create'
                   AND ref_id = $2
             ORDER BY id DESC
             LIMIT 1
            """,
            vault_id, "events/probe",
        )
    assert row is not None
    assert row["kind"] == "collection.create"
    assert row["ref_type"] == "collection"
    assert row["ref_id"] == "events/probe"
    assert row["actor_id"] == "bob"


@pytest.mark.asyncio
async def test_create_unknown_vault_raises_not_found(service):
    from app.exceptions import NotFoundError

    missing = f"_no_such_vault_{uuid.uuid4().hex[:8]}"
    with pytest.raises(NotFoundError):
        await service.create(
            vault=missing, path="x", summary=None, agent_id=None,
        )


# ── delete ────────────────────────────────────────────────────────


class _FakeGit:
    """Records `delete_paths_bulk` calls so cascade tests can assert on
    the git side without touching real bare repos. The unit tests stub
    this in via monkeypatch; the integration suite (Task 7 E2E) exercises
    the real `GitService` path end-to-end.
    """

    def __init__(self):
        self.calls: list[dict] = []

    def delete_paths_bulk(self, *, vault_name, file_paths, message):
        self.calls.append(
            {"vault_name": vault_name, "file_paths": list(file_paths), "message": message}
        )
        return "deadbeef"


@pytest_asyncio.fixture
async def service_with_fake_git(pool, monkeypatch):
    """A fresh service constructed via `CollectionService(git=<fake>)`.

    Mirrors the `service` fixture's `get_pool` monkeypatch (so the new
    instance hits the test pool) but goes through constructor injection
    for `git` — matches the idiom used by `DocumentService` and avoids
    the real `GitService`'s `/data/vaults` mkdir.
    """
    from app.services import collection_service as cs

    async def _fake_get_pool():
        return pool

    monkeypatch.setattr(cs, "get_pool", _fake_get_pool)
    fake = _FakeGit()
    return cs.CollectionService(git=fake), fake


@pytest.mark.asyncio
async def test_delete_empty(service_with_fake_git, vault_name, pool, vault_id):
    service, fake = service_with_fake_git
    await service.create(
        vault=vault_name, path="empty", summary="nothing", agent_id="alice",
    )

    out = await service.delete(
        vault=vault_name, path="empty", recursive=False, agent_id="alice",
    )
    assert out == {
        "ok": True,
        "collection": "empty",
        "deleted_docs": 0,
        "deleted_files": 0,
        "deleted_sub_collections": 0,
    }
    # No docs => no git commit attempted.
    assert fake.calls == []

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM collections WHERE vault_id=$1 AND path=$2",
            vault_id, "empty",
        )
    assert row is None

    # Second delete must surface NotFoundError (idempotency is *not*
    # the contract — explicit delete on a missing collection is a 404).
    from app.exceptions import NotFoundError
    with pytest.raises(NotFoundError):
        await service.delete(
            vault=vault_name, path="empty", recursive=False, agent_id="alice",
        )


async def _seed_doc_under(pool, vault_id, vault_name, coll_path, doc_path):
    """Insert a collection + a document directly. Mirrors the pattern in
    test_collection_repo.test_list_docs_under_returns_only_prefix_matches.
    Returns (collection_id, doc_id)."""
    from datetime import datetime, timezone

    from app.repositories.document_repo import CollectionRepository

    coll_repo = CollectionRepository(pool)
    cid, *_ = await coll_repo.create_empty(vault_id, coll_path)
    doc_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO documents (id, vault_id, collection_id, path, title)
            VALUES ($1, $2, $3, $4, $5)
            """,
            doc_id, vault_id, cid, doc_path, doc_path.rsplit("/", 1)[-1],
        )
    await coll_repo.increment_count(cid, datetime.now(timezone.utc))
    return cid, doc_id


@pytest.mark.asyncio
async def test_delete_non_empty_without_recursive_raises(
    service_with_fake_git, vault_name, pool, vault_id
):
    from app.services.collection_service import CollectionNotEmptyError

    service, fake = service_with_fake_git
    await _seed_doc_under(pool, vault_id, vault_name, "specs", "specs/api.md")

    with pytest.raises(CollectionNotEmptyError) as ei:
        await service.delete(
            vault=vault_name, path="specs", recursive=False, agent_id="alice",
        )
    assert ei.value.doc_count >= 1
    assert ei.value.file_count == 0
    assert ei.value.sub_collection_count == 0
    # No git commit on the abort path.
    assert fake.calls == []

    # Row must still be there — abort is non-destructive.
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM collections WHERE vault_id=$1 AND path=$2",
            vault_id, "specs",
        )
    assert row is not None


@pytest.mark.asyncio
async def test_delete_cascade_removes_docs_files_row(
    service_with_fake_git, vault_name, pool, vault_id
):
    from app.repositories.document_repo import CollectionRepository

    service, fake = service_with_fake_git
    _cid, doc_id = await _seed_doc_under(
        pool, vault_id, vault_name, "drop-me", "drop-me/api.md",
    )
    # Add a file under the same collection so the cascade also exercises
    # the vault_files / s3_delete_outbox path.
    file_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO vault_files (id, vault_id, collection, name, s3_key)
            VALUES ($1, $2, $3, $4, $5)
            """,
            file_id, vault_id, "drop-me", "logo.png", f"k/{file_id}",
        )

    out = await service.delete(
        vault=vault_name, path="drop-me", recursive=True, agent_id="alice",
    )
    assert out["ok"] is True
    assert out["collection"] == "drop-me"
    assert out["deleted_docs"] == 1
    assert out["deleted_files"] == 1

    # Git was called exactly once with the doc path (files are S3-only).
    assert len(fake.calls) == 1
    assert fake.calls[0]["vault_name"] == vault_name
    assert fake.calls[0]["file_paths"] == ["drop-me/api.md"]
    assert "delete-collection" in fake.calls[0]["message"]

    # collections row gone.
    coll_repo = CollectionRepository(pool)
    rows = await coll_repo.list_by_vault(vault_id)
    assert "drop-me" not in {r["path"] for r in rows}

    # Document and file rows are gone too.
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT 1 FROM documents WHERE id=$1", doc_id,
        ) is None
        assert await conn.fetchval(
            "SELECT 1 FROM vault_files WHERE id=$1", file_id,
        ) is None
        # S3 outbox enqueued for the file.
        s3_row = await conn.fetchrow(
            "SELECT s3_key FROM s3_delete_outbox WHERE s3_key=$1",
            f"k/{file_id}",
        )
    assert s3_row is not None


@pytest.mark.asyncio
async def test_delete_cascade_emits_event(
    service_with_fake_git, vault_name, pool, vault_id
):
    service, _fake = service_with_fake_git
    await _seed_doc_under(
        pool, vault_id, vault_name, "loud", "loud/note.md",
    )

    await service.delete(
        vault=vault_name, path="loud", recursive=True, agent_id="bob",
    )

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT kind, ref_type, ref_id, actor_id, payload
              FROM events
             WHERE vault_id = $1 AND kind = 'collection.delete'
                   AND ref_id = $2
             ORDER BY id DESC
             LIMIT 1
            """,
            vault_id, "loud",
        )
    assert row is not None
    assert row["kind"] == "collection.delete"
    assert row["ref_type"] == "collection"
    assert row["ref_id"] == "loud"
    assert row["actor_id"] == "bob"
    # Payload is JSON text on this column — decode and assert counts.
    import json
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["vault"] == vault_name
    assert payload["path"] == "loud"
    assert payload["deleted_docs"] == 1
    assert payload["deleted_files"] == 0


@pytest.mark.asyncio
async def test_delete_unknown_collection_raises_not_found(
    service_with_fake_git, vault_name
):
    from app.exceptions import NotFoundError

    service, _fake = service_with_fake_git
    with pytest.raises(NotFoundError):
        await service.delete(
            vault=vault_name, path="never-existed", recursive=False, agent_id=None,
        )


@pytest.mark.asyncio
async def test_delete_nested_parent_without_recursive_raises(
    service_with_fake_git, vault_name, pool, vault_id
):
    """Reproduces the nested-parent bug: only `test/test` exists (no
    row at `test`), client calls delete on `test` without recursive.
    The old single-row semantics returned 404; the new prefix semantics
    must return `CollectionNotEmptyError(0, 0, 1)` because the
    sub-collection lives under the prefix."""
    from app.repositories.document_repo import CollectionRepository
    from app.services.collection_service import CollectionNotEmptyError

    service, fake = service_with_fake_git
    coll_repo = CollectionRepository(pool)
    # Create ONLY the nested row.
    await coll_repo.create_empty(vault_id, "test/test")

    with pytest.raises(CollectionNotEmptyError) as ei:
        await service.delete(
            vault=vault_name, path="test", recursive=False, agent_id="alice",
        )
    assert ei.value.doc_count == 0
    assert ei.value.file_count == 0
    assert ei.value.sub_collection_count == 1
    # No git commit on the abort path.
    assert fake.calls == []

    # The nested row must still be there — abort is non-destructive.
    rows = await coll_repo.list_by_vault(vault_id)
    paths = {r["path"] for r in rows}
    assert "test/test" in paths
    # And `test` itself still has no row.
    assert "test" not in paths


@pytest.mark.asyncio
async def test_delete_nested_parent_recursive_removes_subrows(
    service_with_fake_git, vault_name, pool, vault_id
):
    """Recursive delete of a nested-parent prefix removes every
    sub-collection row under it even when no row exists at the
    prefix itself."""
    from app.repositories.document_repo import CollectionRepository

    service, _fake = service_with_fake_git
    coll_repo = CollectionRepository(pool)
    await coll_repo.create_empty(vault_id, "test/test")
    await coll_repo.create_empty(vault_id, "test/other")

    out = await service.delete(
        vault=vault_name, path="test", recursive=True, agent_id="alice",
    )
    assert out["ok"] is True
    assert out["collection"] == "test"
    assert out["deleted_docs"] == 0
    assert out["deleted_files"] == 0
    assert out["deleted_sub_collections"] >= 2

    rows = await coll_repo.list_by_vault(vault_id)
    paths = {r["path"] for r in rows}
    assert "test/test" not in paths
    assert "test/other" not in paths
    assert "test" not in paths


@pytest.mark.asyncio
async def test_delete_truly_missing_raises_not_found(
    service_with_fake_git, vault_name
):
    """Path with no row at it AND no descendant rows / docs / files
    must still surface NotFoundError under prefix semantics."""
    from app.exceptions import NotFoundError

    service, _fake = service_with_fake_git
    with pytest.raises(NotFoundError):
        await service.delete(
            vault=vault_name,
            path="totally-absent",
            recursive=False,
            agent_id=None,
        )
    # Even recursive=True on a truly empty prefix should 404 — there's
    # nothing to delete.
    with pytest.raises(NotFoundError):
        await service.delete(
            vault=vault_name,
            path="totally-absent",
            recursive=True,
            agent_id=None,
        )


@pytest.mark.asyncio
async def test_delete_recursive_blocks_concurrent_put(
    service_with_fake_git, vault_name, pool, vault_id
):
    """A concurrent `akb_put` racing with a recursive cascade must NOT
    leave orphan documents pointing at a deleted collection_id.

    The simulation drives both sides explicitly: a background task
    starts the cascade; once it has acquired the `FOR UPDATE` lock and
    is in the middle of its commit (we don't have a deterministic
    pause point, so we approximate by giving the cascade head-start +
    using a separate connection to call `get_or_create`), the racer
    attempts the same path. Because we hold the row lock for the
    entire TX, `get_or_create` must block; after the cascade commits,
    the racer sees the row gone and re-inserts with a fresh id —
    never reusing the doomed id.

    The crucial post-condition: no document has `collection_id`
    referencing a now-absent collections row.
    """
    import asyncio as _aio

    from app.repositories.document_repo import CollectionRepository

    service, _fake = service_with_fake_git
    coll_repo = CollectionRepository(pool)

    # Seed: one pre-existing doc under "race-me" so cascade has work.
    _cid_before, _doc_id = await _seed_doc_under(
        pool, vault_id, vault_name, "race-me", "race-me/old.md",
    )

    # Capture the pre-cascade collection id; the racer must NOT end up
    # with the same id (it should observe the row gone and re-insert).
    async with pool.acquire() as conn:
        pre_cid = await conn.fetchval(
            "SELECT id FROM collections WHERE vault_id=$1 AND path=$2",
            vault_id, "race-me",
        )

    cascade_task = _aio.create_task(
        service.delete(
            vault=vault_name, path="race-me", recursive=True, agent_id="alice",
        )
    )
    # Yield so the cascade can open its TX + acquire FOR UPDATE.
    await _aio.sleep(0.05)

    # Racer: try to grab/insert the same path. Should block until
    # cascade commits, then re-insert with a fresh id.
    racer_cid = await coll_repo.get_or_create(vault_id, "race-me")

    out = await cascade_task
    assert out["deleted_docs"] >= 1

    # Critical invariant: no document points at a deleted collection_id.
    async with pool.acquire() as conn:
        orphans = await conn.fetchval(
            """
            SELECT COUNT(*) FROM documents d
             WHERE d.vault_id = $1
               AND d.collection_id IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM collections c WHERE c.id = d.collection_id
               )
            """,
            vault_id,
        )
    assert orphans == 0, "cascade left orphan documents pointing at a deleted collection"

    # If the racer ran after cascade committed, it should have a NEW id;
    # the doomed id must be gone from collections.
    async with pool.acquire() as conn:
        pre_still_present = await conn.fetchval(
            "SELECT 1 FROM collections WHERE id = $1", pre_cid,
        )
    assert pre_still_present is None
    assert racer_cid != pre_cid
