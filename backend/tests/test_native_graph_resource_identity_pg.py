"""Real-PostgreSQL regressions for the graph's endpoint identity on the native arm.

The `edges` table names an endpoint by the URI its path spells. On the bare-Git
arm that is identity: the `documents` row and the path it owns die in the same
transaction. On `postgres_native` it is not — identity is
`native_resources.resource_id`, `current_path` is mutable, a freed path can be
taken over by a different resource, and the graph work runs after the
authoritative commit. Three release blockers were the same mismatch seen from
three sides:

- akb#654 — a delayed delete cleared the URI and erased the links of the
  document that had since taken the path.
- akb#655 — two moves whose post-commit hooks finished out of order left the
  endpoint on the intermediate path, because the hook replayed an old→new step.
- akb#656 — a non-URI reference (`[B](a.md)`, `[[a.md]]`, a bare frontmatter
  entry) was resolved against the legacy catalog the native arm never writes,
  so a native-only target produced no edge at all.

These run against a dedicated PostgreSQL 16 through the same `_fresh_database`
helper the derived-pipeline concurrency tests use, with real native revision
writes and real graph SQL. Set `AKB_TEST_DSN` to an instance whose user can
create and drop databases; `REQUIRE_REAL_PG=1` turns a missing instance from a
skip into a failure.
"""

from __future__ import annotations

import pathlib
import uuid

import asyncpg

import pytest

import tests.conftest  # noqa: F401  — isolated test config for this revision

from app.services import document_counters
from app.services.kg_service import store_document_relations
from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.native_derived_worker import NativeDerivedWorker
from app.services.native_document_service import NativeDocumentService
from app.services.native_revision_service import NativeRevisionService
from tests.concurrency.test_native_derived_pipeline import _fresh_database

pytestmark = pytest.mark.asyncio

VAULT = "review"


def _args(vault_id, path, actor: str = "review") -> dict:
    return dict(
        namespace_id=vault_id, surface="document", path=path,
        actor=actor, mutation_id=uuid.uuid4(),
    )


async def _vault(pool, name=VAULT):
    async with pool.acquire() as conn:
        owner = await conn.fetchval(
            "INSERT INTO users(username,email,password_hash) "
            "VALUES($1,$2,'disabled') RETURNING id",
            f"{name}-owner", f"{name}@example.invalid",
        )
        return await conn.fetchval(
            "INSERT INTO vaults(name,git_path,owner_id) VALUES($1,$2,$3) RETURNING id",
            name, f"/tmp/unused-{name}.git", owner,
        )


async def _setup(pool, monkeypatch=None):
    """A native vault with one `source.md` to hang links off."""
    if monkeypatch is not None:
        monkeypatch.setattr(
            document_counters, "native_documents_are_authoritative", lambda: True,
        )
    vault_id = await _vault(pool)
    native = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
    source = await native.create_text(**_args(vault_id, "source.md"), payload="source")
    return vault_id, native, NativeDocumentService(pool=pool), source


def _doc(path, vault=VAULT):
    return f"akb://{vault}/doc/{path}"


async def _explicit_edge(pool, vault_id, *, source, target, resource_ids=(None, None)):
    """Seed an explicit edge. ``resource_ids`` None models a row written before
    identity stamping — the state every deployed database is in at upgrade."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO edges(vault_id,source_uri,target_uri,relation_type,"
            "source_type,target_type,kind,source_resource_id,target_resource_id) "
            "VALUES($1,$2,$3,'related_to','doc','doc','explicit',$4,$5)",
            vault_id, source, target, resource_ids[0], resource_ids[1],
        )


async def _edges(pool):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT source_uri, target_uri, kind FROM edges ORDER BY source_uri, target_uri"
        )
    return [(r["source_uri"], r["target_uri"]) for r in rows]


async def _intent(pool, revision_id):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM native_invalidation_intents WHERE revision_id=$1", revision_id,
        )
    intent = dict(row)
    intent["surface"] = "document"
    return intent


async def _apply(worker, pool, revision_id):
    """Apply one resource's derived rewrite out of band, as a delayed worker would."""
    intent = await _intent(pool, revision_id)
    head = await worker._head(intent["resource_id"])
    if head is not None and head["lifecycle"] == "deleted":
        await worker._apply_delete(intent)
    else:
        await worker._apply_live(intent, head)


# ── akb#654 — a delayed delete must not reach a replacement ─────────


async def test_a_delayed_delete_keeps_the_replacements_links(monkeypatch):
    async with _fresh_database() as pool:
        vault_id, native, _, _ = await _setup(pool, monkeypatch)
        original = await native.create_text(**_args(vault_id, "a.md"), payload="old")
        deleted = await native.delete_resource(
            **_args(vault_id, "a.md"), expected_revision_id=original.revision_id,
        )
        replacement = await native.create_text(**_args(vault_id, "a.md"), payload="new")
        assert replacement.resource_id != original.resource_id
        await _explicit_edge(
            pool, vault_id, source=_doc("source.md"), target=_doc("a.md"),
            resource_ids=(None, replacement.resource_id),
        )

        await NativeDerivedWorker(pool)._apply_delete(await _intent(pool, deleted.revision_id))

        assert await _edges(pool) == [(_doc("source.md"), _doc("a.md"))]


async def test_a_delete_removes_both_directions_when_the_path_is_free(monkeypatch):
    async with _fresh_database() as pool:
        vault_id, native, _, _ = await _setup(pool, monkeypatch)
        doomed = await native.create_text(**_args(vault_id, "a.md"), payload="body")
        other = await native.create_text(**_args(vault_id, "other.md"), payload="body")
        await _explicit_edge(
            pool, vault_id, source=_doc("source.md"), target=_doc("a.md"),
            resource_ids=(None, doomed.resource_id),
        )
        await _explicit_edge(
            pool, vault_id, source=_doc("a.md"), target=_doc("other.md"),
            resource_ids=(doomed.resource_id, other.resource_id),
        )
        deleted = await native.delete_resource(
            **_args(vault_id, "a.md"), expected_revision_id=doomed.revision_id,
        )

        await NativeDerivedWorker(pool)._apply_delete(await _intent(pool, deleted.revision_id))

        assert await _edges(pool) == []


async def test_a_retried_delete_intent_is_idempotent(monkeypatch):
    """Delivery is at-least-once, so the cleanup has to survive a second run."""
    async with _fresh_database() as pool:
        vault_id, native, _, _ = await _setup(pool, monkeypatch)
        doomed = await native.create_text(**_args(vault_id, "a.md"), payload="body")
        await _explicit_edge(
            pool, vault_id, source=_doc("source.md"), target=_doc("a.md"),
            resource_ids=(None, doomed.resource_id),
        )
        deleted = await native.delete_resource(
            **_args(vault_id, "a.md"), expected_revision_id=doomed.revision_id,
        )
        worker = NativeDerivedWorker(pool)
        intent = await _intent(pool, deleted.revision_id)

        await worker._apply_delete(intent)
        replacement = await native.create_text(**_args(vault_id, "a.md"), payload="new")
        await _explicit_edge(
            pool, vault_id, source=_doc("source.md"), target=_doc("a.md"),
            resource_ids=(None, replacement.resource_id),
        )
        await worker._apply_delete(intent)

        assert await _edges(pool) == [(_doc("source.md"), _doc("a.md"))]


async def test_a_delete_removes_rows_that_predate_identity_stamping(monkeypatch):
    """The upgrade case: an unclaimed row at a path nobody holds is still cleared.

    Skipping every URI-keyed row when identity is absent would swap one defect
    for its mirror image — a deleted document keeping its links forever.
    """
    async with _fresh_database() as pool:
        vault_id, native, _, _ = await _setup(pool, monkeypatch)
        doomed = await native.create_text(**_args(vault_id, "a.md"), payload="body")
        await _explicit_edge(pool, vault_id, source=_doc("source.md"), target=_doc("a.md"))
        deleted = await native.delete_resource(
            **_args(vault_id, "a.md"), expected_revision_id=doomed.revision_id,
        )

        await NativeDerivedWorker(pool)._apply_delete(await _intent(pool, deleted.revision_id))

        assert await _edges(pool) == []


async def test_an_unstamped_row_at_a_reused_path_survives_the_old_delete(monkeypatch):
    """The same upgrade case, with the path taken over — the row is not ours."""
    async with _fresh_database() as pool:
        vault_id, native, _, _ = await _setup(pool, monkeypatch)
        original = await native.create_text(**_args(vault_id, "a.md"), payload="old")
        deleted = await native.delete_resource(
            **_args(vault_id, "a.md"), expected_revision_id=original.revision_id,
        )
        await native.create_text(**_args(vault_id, "a.md"), payload="new")
        await _explicit_edge(pool, vault_id, source=_doc("source.md"), target=_doc("a.md"))

        await NativeDerivedWorker(pool)._apply_delete(await _intent(pool, deleted.revision_id))

        assert await _edges(pool) == [(_doc("source.md"), _doc("a.md"))]


async def test_a_delete_leaves_a_legacy_documents_edges_at_the_same_path(monkeypatch):
    """A cutover can leave a legacy catalog row at the path a native resource holds.

    `_resolve_document_endpoint` answers with the legacy row first, so an
    unstamped edge at that URI is the LEGACY document's. The unconditional
    URI sweep this replaces erased it along with the native resource.
    """
    async with _fresh_database() as pool:
        vault_id, native, _, _ = await _setup(pool, monkeypatch)
        doomed = await native.create_text(**_args(vault_id, "a.md"), payload="body")
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO documents(vault_id, path, title) VALUES($1,'a.md','Legacy')",
                vault_id,
            )
        await _explicit_edge(pool, vault_id, source=_doc("source.md"), target=_doc("a.md"))
        deleted = await native.delete_resource(
            **_args(vault_id, "a.md"), expected_revision_id=doomed.revision_id,
        )

        await NativeDerivedWorker(pool)._apply_delete(await _intent(pool, deleted.revision_id))

        assert await _edges(pool) == [(_doc("source.md"), _doc("a.md"))]


# ── akb#655 — move hooks converge whatever order they finish in ─────


@pytest.mark.parametrize("reordered", [False, True])
async def test_move_hooks_end_at_the_current_path_in_either_order(monkeypatch, reordered):
    async with _fresh_database() as pool:
        vault_id, native, facade, _ = await _setup(pool, monkeypatch)
        a = await native.create_text(**_args(vault_id, "a.md"), payload="body")
        await _explicit_edge(
            pool, vault_id, source=_doc("source.md"), target=_doc("a.md"),
            resource_ids=(None, a.resource_id),
        )

        first = await native.move_text(
            **_args(vault_id, "a.md"), path_to="b.md",
            expected_revision_id=a.revision_id,
        )
        if not reordered:
            await facade._relink_moved_edges(
                vault_id, VAULT, old_path="a.md", new_path="b.md",
                resource_id=first.resource_id,
            )
        second = await native.move_text(
            **_args(vault_id, "b.md"), path_to="c.md",
            expected_revision_id=first.revision_id,
        )
        await facade._relink_moved_edges(
            vault_id, VAULT, old_path="b.md", new_path="c.md",
            resource_id=second.resource_id,
        )
        if reordered:
            # The first request resumes and completes its hook last.
            await facade._relink_moved_edges(
                vault_id, VAULT, old_path="a.md", new_path="b.md",
                resource_id=first.resource_id,
            )

        await NativeDerivedWorker(pool).settle(namespace_id=vault_id, timeout_seconds=15)

        assert await _edges(pool) == [(_doc("source.md"), _doc("c.md"))]


async def test_a_move_hook_that_never_ran_is_recovered_by_the_next_one(monkeypatch):
    """Recovery after a post-commit interruption.

    The A→B hook is lost entirely — the process died between commit and hook.
    The B→C hook writes the head path for the resource, not the B→C step, so
    the endpoint lands on `c.md` without the lost hook ever running.
    """
    async with _fresh_database() as pool:
        vault_id, native, facade, _ = await _setup(pool, monkeypatch)
        a = await native.create_text(**_args(vault_id, "a.md"), payload="body")
        await _explicit_edge(
            pool, vault_id, source=_doc("source.md"), target=_doc("a.md"),
            resource_ids=(None, a.resource_id),
        )
        first = await native.move_text(
            **_args(vault_id, "a.md"), path_to="b.md",
            expected_revision_id=a.revision_id,
        )
        second = await native.move_text(
            **_args(vault_id, "b.md"), path_to="c.md",
            expected_revision_id=first.revision_id,
        )
        await facade._relink_moved_edges(
            vault_id, VAULT, old_path="b.md", new_path="c.md",
            resource_id=second.resource_id,
        )

        assert await _edges(pool) == [(_doc("source.md"), _doc("c.md"))]


async def test_the_derived_worker_recovers_an_endpoint_no_hook_ever_carried(monkeypatch):
    """Durable recovery: the facade hook never runs at all.

    A crash between the authoritative commit and the post-commit hook leaves
    nobody to carry the explicit link, and nothing retries the hook. The
    invalidation intent IS durable, so the worker repoints the endpoints from
    the head path it has already locked.
    """
    async with _fresh_database() as pool:
        vault_id, native, _, _ = await _setup(pool, monkeypatch)
        a = await native.create_text(**_args(vault_id, "a.md"), payload="body")
        await _explicit_edge(
            pool, vault_id, source=_doc("source.md"), target=_doc("a.md"),
            resource_ids=(None, a.resource_id),
        )
        await native.move_text(
            **_args(vault_id, "a.md"), path_to="b.md",
            expected_revision_id=a.revision_id,
        )
        # No facade hook at all.
        await NativeDerivedWorker(pool).settle(namespace_id=vault_id, timeout_seconds=15)

        assert await _edges(pool) == [(_doc("source.md"), _doc("b.md"))]


async def test_a_move_then_delete_interleaving_leaves_no_edge(monkeypatch):
    async with _fresh_database() as pool:
        vault_id, native, facade, _ = await _setup(pool, monkeypatch)
        a = await native.create_text(**_args(vault_id, "a.md"), payload="body")
        await _explicit_edge(
            pool, vault_id, source=_doc("source.md"), target=_doc("a.md"),
            resource_ids=(None, a.resource_id),
        )
        moved = await native.move_text(
            **_args(vault_id, "a.md"), path_to="b.md",
            expected_revision_id=a.revision_id,
        )
        deleted = await native.delete_resource(
            **_args(vault_id, "b.md"), expected_revision_id=moved.revision_id,
        )
        # The move's hook arrives AFTER the delete committed.
        await facade._relink_moved_edges(
            vault_id, VAULT, old_path="a.md", new_path="b.md",
            resource_id=moved.resource_id,
        )
        await NativeDerivedWorker(pool)._apply_delete(await _intent(pool, deleted.revision_id))

        assert await _edges(pool) == []


async def test_a_delayed_move_rewrite_keeps_the_new_owners_implicit_links(monkeypatch):
    """The derived rewrite's old-path sweep was the same defect as akb#654.

    Resource X moves off `a.md`, resource Y takes it and settles first. X's
    delayed rewrite must not clear the implicit rows now sitting at `a.md`.
    """
    async with _fresh_database() as pool:
        vault_id, native, _, _ = await _setup(pool, monkeypatch)
        await native.create_text(**_args(vault_id, "target.md"), payload="target")
        worker = NativeDerivedWorker(pool)
        x = await native.create_text(
            **_args(vault_id, "a.md"), payload=f"[t]({_doc('target.md')})",
        )
        await worker.settle(namespace_id=vault_id, timeout_seconds=15)
        moved = await native.move_text(
            **_args(vault_id, "a.md"), path_to="moved.md",
            expected_revision_id=x.revision_id,
        )
        y = await native.create_text(
            **_args(vault_id, "a.md"), payload=f"[t]({_doc('target.md')})",
        )
        assert y.resource_id != moved.resource_id

        await _apply(worker, pool, y.revision_id)
        await _apply(worker, pool, moved.revision_id)

        assert await _edges(pool) == [
            (_doc("a.md"), _doc("target.md")),
            (_doc("moved.md"), _doc("target.md")),
        ]


# ── akb#656 — non-URI references resolve through the active authority ──


@pytest.mark.parametrize(
    "body",
    ["[B](a.md)", "[[a.md]]", "[[a.md|Label]]", "[B](./a.md)", f"[B]({_doc('a.md')})"],
)
async def test_every_reference_form_links_a_native_only_document(monkeypatch, body):
    async with _fresh_database() as pool:
        vault_id, native, _, _ = await _setup(pool, monkeypatch)
        await native.create_text(**_args(vault_id, "a.md"), payload="target")
        async with pool.acquire() as conn:
            stored = await store_document_relations(
                conn, vault_id, VAULT, "source.md", [], [], [], body,
            )
        assert stored == 1, body
        assert await _edges(pool) == [(_doc("source.md"), _doc("a.md"))]


async def test_a_bare_frontmatter_reference_links_a_native_only_document(monkeypatch):
    async with _fresh_database() as pool:
        vault_id, native, _, _ = await _setup(pool, monkeypatch)
        await native.create_text(**_args(vault_id, "a.md"), payload="target")
        async with pool.acquire() as conn:
            stored = await store_document_relations(
                conn, vault_id, VAULT, "source.md", ["a.md"], [], [], "",
            )
        assert stored == 1
        assert await _edges(pool) == [(_doc("source.md"), _doc("a.md"))]


async def test_a_suffix_reference_resolves_a_native_document_in_a_collection(monkeypatch):
    async with _fresh_database() as pool:
        vault_id, native, _, _ = await _setup(pool, monkeypatch)
        await native.create_text(**_args(vault_id, "specs/a.md"), payload="target")
        async with pool.acquire() as conn:
            stored = await store_document_relations(
                conn, vault_id, VAULT, "source.md", [], [], [], "[B](a.md)",
            )
        assert stored == 1
        assert await _edges(pool) == [
            (_doc("source.md"), f"akb://{VAULT}/coll/specs/doc/a.md"),
        ]


async def test_an_ambiguous_suffix_reference_stores_no_edge(monkeypatch):
    """Two candidates and no way to choose: refuse rather than pick one.

    The legacy arm returns whichever row came back first — its own docstring
    calls that a wrong-doc magnet. The native arm does not inherit it.
    """
    async with _fresh_database() as pool:
        vault_id, native, _, _ = await _setup(pool, monkeypatch)
        await native.create_text(**_args(vault_id, "specs/a.md"), payload="one")
        await native.create_text(**_args(vault_id, "notes/a.md"), payload="two")
        async with pool.acquire() as conn:
            stored = await store_document_relations(
                conn, vault_id, VAULT, "source.md", [], [], [], "[B](a.md)",
            )
        assert stored == 0
        assert await _edges(pool) == []


async def test_a_reference_to_a_deleted_native_document_stores_no_edge(monkeypatch):
    async with _fresh_database() as pool:
        vault_id, native, _, _ = await _setup(pool, monkeypatch)
        gone = await native.create_text(**_args(vault_id, "a.md"), payload="target")
        await native.delete_resource(
            **_args(vault_id, "a.md"), expected_revision_id=gone.revision_id,
        )
        async with pool.acquire() as conn:
            stored = await store_document_relations(
                conn, vault_id, VAULT, "source.md", [], [], [], "[B](a.md)",
            )
        assert stored == 0
        assert await _edges(pool) == []


async def test_a_reference_to_a_moved_native_document_resolves_to_its_current_path(monkeypatch):
    async with _fresh_database() as pool:
        vault_id, native, _, _ = await _setup(pool, monkeypatch)
        a = await native.create_text(**_args(vault_id, "a.md"), payload="target")
        await native.move_text(
            **_args(vault_id, "a.md"), path_to="specs/b.md",
            expected_revision_id=a.revision_id,
        )
        async with pool.acquire() as conn:
            stored = await store_document_relations(
                conn, vault_id, VAULT, "source.md", [], [], [], "[B](specs/b.md)",
            )
        assert stored == 1
        assert await _edges(pool) == [
            (_doc("source.md"), f"akb://{VAULT}/coll/specs/doc/b.md"),
        ]


async def test_a_reference_does_not_reach_another_vaults_native_document(monkeypatch):
    async with _fresh_database() as pool:
        vault_id, native, _, _ = await _setup(pool, monkeypatch)
        other_id = await _vault(pool, "other")
        other = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        await other.create_text(**_args(other_id, "a.md"), payload="target")
        async with pool.acquire() as conn:
            stored = await store_document_relations(
                conn, vault_id, VAULT, "source.md", [], [], [],
                "[B](a.md)\n[C](akb://other/doc/a.md)",
            )
        assert stored == 0
        assert await _edges(pool) == []


async def test_a_legacy_installation_never_links_through_the_native_ledger(monkeypatch):
    """Preserve legacy-mode semantics: the selector still gates the new arm."""
    async with _fresh_database() as pool:
        vault_id, native, _, _ = await _setup(pool, monkeypatch)
        await native.create_text(**_args(vault_id, "a.md"), payload="target")
        monkeypatch.setattr(
            document_counters, "native_documents_are_authoritative", lambda: False,
        )
        async with pool.acquire() as conn:
            stored = await store_document_relations(
                conn, vault_id, VAULT, "source.md", [], [], [], "[B](a.md)",
            )
        assert stored == 0
        assert await _edges(pool) == []


async def test_a_legacy_catalog_reference_still_resolves_on_a_native_installation(monkeypatch):
    """A cutover leaves pre-cutover documents in `documents`; they stay linkable."""
    async with _fresh_database() as pool:
        vault_id, _, _, _ = await _setup(pool, monkeypatch)
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO documents(vault_id, path, title) "
                "VALUES($1,'legacy.md','Legacy')",
                vault_id,
            )
            stored = await store_document_relations(
                conn, vault_id, VAULT, "source.md", [], [], [], "[B](legacy.md)",
            )
        assert stored == 1
        async with pool.acquire() as conn:
            identity = await conn.fetchval(
                "SELECT target_resource_id FROM edges WHERE target_uri = $1",
                _doc("legacy.md"),
            )
        assert identity is None, "a legacy endpoint must not be stamped with a native id"


# ── migration 112 ──────────────────────────────────────────────────


async def test_the_migration_backfill_stamps_an_existing_unclaimed_edge(monkeypatch):
    """The backfill is the upgrade path, and a fresh database never exercises it.

    `_fresh_database` applies every migration to an EMPTY database, so the
    batching, the array carry and the `IS NULL` guard all run over nothing.
    Re-running the migration over a database that already has rows is what
    tells us the backfill works — and that it is idempotent, which the boot
    sequence relies on when a migration is retried after a lock timeout.
    """
    from app.db.migrations import __name__ as _  # noqa: F401 — package import
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "migration_112",
        pathlib.Path(__file__).resolve().parents[1]
        / "app" / "db" / "migrations" / "112_edges_resource_identity.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async with _fresh_database() as pool:
        vault_id, native, _, source = await _setup(pool, monkeypatch)
        target = await native.create_text(**_args(vault_id, "a.md"), payload="target")
        # A row exactly as it would look before identity stamping existed.
        await _explicit_edge(pool, vault_id, source=_doc("source.md"), target=_doc("a.md"))

        async with pool.acquire() as conn:
            await module.migrate(conn)
            row = await conn.fetchrow(
                "SELECT source_resource_id, target_resource_id FROM edges"
            )
        assert row["source_resource_id"] == source.resource_id
        assert row["target_resource_id"] == target.resource_id

        # Idempotent: a second pass changes nothing and does not fail.
        async with pool.acquire() as conn:
            await module.migrate(conn)
            row = await conn.fetchrow(
                "SELECT source_resource_id, target_resource_id FROM edges"
            )
        assert row["source_resource_id"] == source.resource_id
        assert row["target_resource_id"] == target.resource_id


async def test_the_identity_columns_reject_an_id_no_resource_owns(monkeypatch):
    """The foreign key is real, not decorative."""
    async with _fresh_database() as pool:
        vault_id, native, _, _ = await _setup(pool, monkeypatch)
        await native.create_text(**_args(vault_id, "a.md"), payload="target")
        with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
            await _explicit_edge(
                pool, vault_id, source=_doc("source.md"), target=_doc("a.md"),
                resource_ids=(None, uuid.uuid4()),
            )


# ── the stamp itself ───────────────────────────────────────────────


async def test_an_implicit_edge_records_both_endpoints_identity(monkeypatch):
    async with _fresh_database() as pool:
        vault_id, native, _, source = await _setup(pool, monkeypatch)
        target = await native.create_text(**_args(vault_id, "a.md"), payload="target")
        async with pool.acquire() as conn:
            await store_document_relations(
                conn, vault_id, VAULT, "source.md", [], [], [], "[B](a.md)",
                source.resource_id,
            )
            row = await conn.fetchrow(
                "SELECT source_resource_id, target_resource_id FROM edges"
            )
        assert row["source_resource_id"] == source.resource_id
        assert row["target_resource_id"] == target.resource_id


async def test_an_implicit_rewrite_leaves_an_explicit_edge_explicit(monkeypatch):
    """The `kind` discriminator from migration 028 still protects akb_link rows."""
    async with _fresh_database() as pool:
        vault_id, native, _, source = await _setup(pool, monkeypatch)
        await native.create_text(**_args(vault_id, "a.md"), payload="target")
        await _explicit_edge(
            pool, vault_id, source=_doc("source.md"), target=_doc("a.md"),
        )
        async with pool.acquire() as conn:
            await store_document_relations(
                conn, vault_id, VAULT, "source.md", [], ["a.md"], [], "",
                source.resource_id,
            )
            rows = await conn.fetch("SELECT kind, relation_type FROM edges")
        assert [(r["kind"], r["relation_type"]) for r in rows] == [("explicit", "related_to")]
