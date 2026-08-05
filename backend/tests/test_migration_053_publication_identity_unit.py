"""Live-PostgreSQL contract for migration 053 (publication document identity).

What 053 adds is a *database* guarantee — a composite FK with ON DELETE
CASCADE — so every assertion here goes through SQL that bypasses the
application entirely. That is the point of the change: the Python-level
cleanup paths already work, and a test that drove them would prove nothing
about the case they do not cover (a direct psql session, a later migration,
an admin script). Each cascade and refusal below is issued as raw SQL against
a real database.

Covered:

  * a fresh `init.sql` database and a database that reached the same point
    through the migration end up with the identical catalog for both tables —
    compared from `pg_constraint` / `pg_index`, not by reading the files;
  * deleting a `documents` row in SQL takes its publications with it;
  * the composite FK refuses a publication bound to a document in another
    vault, which a single-column FK would accept;
  * the backfill binds only what is unambiguous — a vault-name mismatch, a
    path with no document, a path whose document postdates the publication,
    and an unreadable URI are all left NULL, and no row is ever deleted;
  * re-running changes nothing, at the level of both catalog and rows;
  * an invalid index left by a cancelled `CREATE INDEX CONCURRENTLY` is
    replaced rather than silently accepted, a valid one is adopted without a
    rebuild, and an unrelated index wearing the name fails loudly;
  * migration 022, which drops a column of the same name, does not eat this
    one on a fresh database.

Runs in a disposable database so it never touches a dev DB's data. Registered
in the `pgvector e2e (live DB)` CI job, which sets `AKB_TEST_DSN` and
`REQUIRE_REAL_PG=1`; without that registration it would skip in both jobs and
be a gate that never fires.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest

pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text()
_MIGRATIONS = _BACKEND / "app" / "db" / "migrations"
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)

# The additions 053 makes. Dropping these from an init.sql database
# reconstructs the pre-053 shape, which is what the migration has to turn
# back into the post-053 shape.
_UNDO_053 = """
ALTER TABLE publications DROP CONSTRAINT publications_document_fk;
DROP INDEX idx_publications_document_id;
ALTER TABLE publications DROP COLUMN document_id;
ALTER TABLE documents DROP CONSTRAINT documents_id_vault_id_key;
"""


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2.0)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


def _load(filename: str):
    """Load a migration from source (NOT import_module, which could honour a
    stale __pycache__ .pyc and mask a source regression)."""
    path = _MIGRATIONS / filename
    spec = importlib.util.spec_from_file_location(f"mig_{filename[:3]}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@asynccontextmanager
async def _fresh_database(*, undo_053: bool = False):
    if not await _can_connect(_DSN):
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"REQUIRE_REAL_PG=1 but Postgres is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")
    admin = await asyncpg.connect(_DSN)
    name = f"akb_pub_identity_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    base, _ = _DSN.rsplit("/", 1)
    conn = await asyncpg.connect(f"{base}/{name}")
    try:
        await conn.execute(_INIT_SQL)
        if undo_053:
            await conn.execute(_UNDO_053)
        yield conn
    finally:
        await conn.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


async def _apply(conn) -> None:
    await _load("053_publication_document_identity.py").migrate(conn=conn)


# ------------------------------------------------------------------
# Fixtures the tests build by hand. Deliberately raw SQL: a publication
# created through the service layer would be correct by construction, and
# these tests exist to describe rows that are not.
# ------------------------------------------------------------------


async def _vault(conn, name: str) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO vaults (name, git_path) VALUES ($1, $2) RETURNING id",
        name, f"/tmp/{name}.git",
    )


async def _document(conn, vault_id: uuid.UUID, path: str, **kw) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO documents (vault_id, path, title, created_at) "
        "VALUES ($1, $2, $3, COALESCE($4, NOW())) RETURNING id",
        vault_id, path, kw.get("title", path), kw.get("created_at"),
    )


async def _publication(conn, vault_id: uuid.UUID, uri: str | None, **kw) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO publications (slug, vault_id, resource_type, resource_uri, created_at) "
        "VALUES ($1, $2, $3, $4, COALESCE($5, NOW())) RETURNING id",
        kw.get("slug", uuid.uuid4().hex[:16]), vault_id,
        kw.get("resource_type", "document"), uri, kw.get("created_at"),
    )


async def _shape(conn) -> dict:
    """The catalog for both tables, in a form two databases can be compared on.

    Constraint definitions come from `pg_get_constraintdef` and index
    definitions from `pg_get_indexdef`, so this compares what the database
    actually built rather than the SQL anyone wrote.
    """
    constraints = await conn.fetch(
        """
        SELECT c.conrelid::regclass::text AS tbl, c.conname::text AS name,
               pg_get_constraintdef(c.oid) AS def, c.convalidated
          FROM pg_constraint c
         WHERE c.conrelid IN ('publications'::regclass, 'documents'::regclass)
         ORDER BY tbl, name
        """
    )
    indexes = await conn.fetch(
        """
        SELECT i.indrelid::regclass::text AS tbl,
               pg_get_indexdef(i.indexrelid) AS def, i.indisvalid
          FROM pg_index i
         WHERE i.indrelid IN ('publications'::regclass, 'documents'::regclass)
         ORDER BY tbl, def
        """
    )
    columns = await conn.fetch(
        """
        SELECT table_name, column_name, data_type, is_nullable
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name IN ('publications', 'documents')
         ORDER BY table_name, column_name
        """
    )
    return {
        "constraints": [tuple(r) for r in constraints],
        "indexes": [tuple(r) for r in indexes],
        "columns": [tuple(r) for r in columns],
    }


async def test_migrated_database_matches_a_fresh_init_sql_database():
    async with _fresh_database() as fresh:
        expected = await _shape(fresh)

    async with _fresh_database(undo_053=True) as migrated:
        # Sanity: the undo really did reconstruct a pre-053 shape.
        assert await _shape(migrated) != expected
        await _apply(migrated)
        assert await _shape(migrated) == expected

    # And every piece is actually present, so a mutual absence cannot pass.
    assert any(
        name == "publications_document_fk"
        and "FOREIGN KEY (document_id, vault_id) REFERENCES documents(id, vault_id) "
            "ON DELETE CASCADE" in definition
        for _, name, definition, _ in expected["constraints"]
    )
    assert any(
        name == "documents_id_vault_id_key" and "UNIQUE (id, vault_id)" in definition
        for _, name, definition, _ in expected["constraints"]
    )
    assert all(valid for *_, valid in expected["indexes"])
    assert all(validated for *_, validated in expected["constraints"])


async def test_init_sql_still_runs_against_a_database_that_predates_the_migration():
    """init.sql is re-executed IN FULL on every boot, BEFORE any migration.

    So every statement in it has to be inert against a database that has not
    reached 053 yet. `CREATE TABLE IF NOT EXISTS` is; a bare `CREATE INDEX` on
    the new column is not — it raises UndefinedColumn, aborts init_db(), and
    the migration that would have added the column never runs. Every boot,
    forever. This test is the reason that statement is guarded.
    """
    async with _fresh_database(undo_053=True) as conn:
        assert await conn.fetchval(
            "SELECT 1 FROM information_schema.columns WHERE table_name = 'publications' "
            "AND column_name = 'document_id'"
        ) is None

        await conn.execute(_INIT_SQL)  # must not raise

        # It must also not have invented the index on a column that is absent.
        assert await conn.fetchval(
            "SELECT to_regclass('public.idx_publications_document_id')"
        ) is None

        await _apply(conn)

        # And once the column is there, a later boot's init.sql is a clean
        # no-op over the shape the migration built.
        shape = await _shape(conn)
        await conn.execute(_INIT_SQL)
        assert await _shape(conn) == shape


async def test_a_document_cannot_be_moved_out_from_under_a_publication():
    """The FK's ON UPDATE is NO ACTION (the default), so the vault half of the
    key cannot be changed to break the pairing either."""
    async with _fresh_database() as conn:
        vault_a = await _vault(conn, f"stay-{uuid.uuid4().hex[:8]}")
        vault_b = await _vault(conn, f"move-{uuid.uuid4().hex[:8]}")
        doc = await _document(conn, vault_a, "notes.md")
        pub = await _publication(conn, vault_a, "akb://x/doc/notes.md")
        await conn.execute(
            "UPDATE publications SET document_id = $2 WHERE id = $1", pub, doc
        )

        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "UPDATE documents SET vault_id = $2 WHERE id = $1", doc, vault_b
            )


async def test_deleting_the_document_row_in_sql_takes_the_publication_with_it():
    async with _fresh_database(undo_053=True) as conn:
        vault = await _vault(conn, f"cascade-{uuid.uuid4().hex[:8]}")
        doc = await _document(conn, vault, "reports/q3.md")
        pub = await _publication(
            conn, vault,
            f"akb://{await conn.fetchval('SELECT name FROM vaults WHERE id=$1', vault)}"
            "/coll/reports/doc/q3.md",
        )
        await _apply(conn)
        assert await conn.fetchval(
            "SELECT document_id FROM publications WHERE id = $1", pub
        ) == doc

        # No service, no repository, no Python-level cleanup — the single
        # statement every application-level defence is unable to see.
        await conn.execute("DELETE FROM documents WHERE id = $1", doc)

        assert await conn.fetchval(
            "SELECT COUNT(*) FROM publications WHERE id = $1", pub
        ) == 0


async def test_composite_fk_refuses_a_document_from_another_vault():
    async with _fresh_database() as conn:
        vault_a = await _vault(conn, f"own-{uuid.uuid4().hex[:8]}")
        vault_b = await _vault(conn, f"other-{uuid.uuid4().hex[:8]}")
        doc_a = await _document(conn, vault_a, "notes.md")
        doc_b = await _document(conn, vault_b, "notes.md")
        pub = await _publication(conn, vault_a, "akb://x/doc/notes.md")

        # Same vault: accepted.
        await conn.execute(
            "UPDATE publications SET document_id = $2 WHERE id = $1", pub, doc_a
        )

        # Other vault: refused. A single-column FK REFERENCES documents(id)
        # would accept this — doc_b exists — which is why the key is composite.
        with pytest.raises(asyncpg.ForeignKeyViolationError) as exc:
            await conn.execute(
                "UPDATE publications SET document_id = $2 WHERE id = $1", pub, doc_b
            )
        assert exc.value.sqlstate == "23503"

        # Inserting one that way is refused too, not just updating.
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "INSERT INTO publications (slug, vault_id, resource_type, document_id) "
                "VALUES ($1, $2, 'document', $3)",
                uuid.uuid4().hex[:16], vault_a, doc_b,
            )

        # And the row that was legitimately bound is untouched by the refusals.
        assert await conn.fetchval(
            "SELECT document_id FROM publications WHERE id = $1", pub
        ) == doc_a


async def test_backfill_binds_only_unambiguous_rows_and_deletes_nothing():
    async with _fresh_database(undo_053=True) as conn:
        own = f"own-{uuid.uuid4().hex[:8]}"
        elsewhere = f"elsewhere-{uuid.uuid4().hex[:8]}"
        vault = await _vault(conn, own)
        other_vault = await _vault(conn, elsewhere)

        bindable_doc = await _document(conn, vault, "reports/q3.md")
        root_doc = await _document(conn, vault, "notes.md")

        bindable = await _publication(conn, vault, f"akb://{own}/coll/reports/doc/q3.md")
        root = await _publication(conn, vault, f"akb://{own}/doc/notes.md")

        # The URI names a vault other than the one the row belongs to, and a
        # document DOES exist at that path in the named vault. Fail closed.
        await _document(conn, other_vault, "shared.md")
        mismatch = await _publication(conn, vault, f"akb://{elsewhere}/doc/shared.md")

        # The same disagreement, with a same-path document in the row's OWN
        # vault as well. This is the case that isolates the vault-name check:
        # the vault predicate in the UPDATE is satisfied here, so only the
        # name comparison can refuse it — and it has to, because resolution
        # refuses to serve a row whose two vault identifiers disagree.
        await _document(conn, other_vault, "both.md")
        await _document(conn, vault, "both.md")
        mismatch_both = await _publication(conn, vault, f"akb://{elsewhere}/doc/both.md")

        # No document at the path at all.
        no_document = await _publication(conn, vault, f"akb://{own}/doc/gone.md")

        # A document exists at the path but was created after the publication,
        # so it cannot be the document the publication was made for.
        reused_at = await conn.fetchval("SELECT NOW() - INTERVAL '1 hour'")
        reused = await _publication(
            conn, vault, f"akb://{own}/doc/reused.md", created_at=reused_at
        )
        await _document(conn, vault, "reused.md")

        # Not a URI this codebase can read.
        unreadable = await _publication(conn, vault, f"akb://{own}/doc/{{template}}.md")

        # Other resource types are not this migration's business.
        table_query = await _publication(
            conn, vault, None, resource_type="table_query"
        )
        file_pub = await _publication(
            conn, vault, f"akb://{own}/file/{uuid.uuid4()}", resource_type="file"
        )

        before = await conn.fetchval("SELECT COUNT(*) FROM publications")
        await _apply(conn)

        bound = {
            r["id"]: r["document_id"]
            for r in await conn.fetch("SELECT id, document_id FROM publications")
        }
        assert bound[bindable] == bindable_doc
        assert bound[root] == root_doc
        for pub in (
            mismatch, mismatch_both, no_document, reused, unreadable,
            table_query, file_pub,
        ):
            assert bound[pub] is None

        # Nothing was deleted: the slug, creator and view count of an
        # unbindable row are the record a human needs to act on it.
        assert await conn.fetchval("SELECT COUNT(*) FROM publications") == before


async def test_rerunning_changes_nothing():
    async with _fresh_database(undo_053=True) as conn:
        own = f"idem-{uuid.uuid4().hex[:8]}"
        vault = await _vault(conn, own)
        await _document(conn, vault, "notes.md")
        await _publication(conn, vault, f"akb://{own}/doc/notes.md")
        await _publication(conn, vault, f"akb://{own}/doc/absent.md")

        await _apply(conn)
        shape = await _shape(conn)
        rows = await conn.fetch("SELECT * FROM publications ORDER BY id")

        await _apply(conn)

        assert await _shape(conn) == shape
        assert await conn.fetch("SELECT * FROM publications ORDER BY id") == rows


async def test_index_left_invalid_by_a_cancelled_build_is_replaced():
    async with _fresh_database(undo_053=True) as conn:
        await conn.execute(
            "CREATE UNIQUE INDEX documents_id_vault_id_key ON documents (id, vault_id)"
        )
        # What a cancelled CREATE INDEX CONCURRENTLY leaves behind. `IF NOT
        # EXISTS` would step over this and the migration would believe it had
        # an index it cannot use.
        await conn.execute(
            "UPDATE pg_index SET indisvalid = false "
            "WHERE indexrelid = 'documents_id_vault_id_key'::regclass"
        )

        await _apply(conn)

        assert await conn.fetchval(
            "SELECT indisvalid FROM pg_index "
            "WHERE indexrelid = 'documents_id_vault_id_key'::regclass"
        ) is True
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM pg_constraint WHERE conname = 'documents_id_vault_id_key'"
        ) == 1


async def test_a_valid_index_built_out_of_band_is_adopted_not_rebuilt():
    async with _fresh_database(undo_053=True) as conn:
        await conn.execute(
            "CREATE UNIQUE INDEX documents_id_vault_id_key ON documents (id, vault_id)"
        )
        before = await conn.fetchval(
            "SELECT 'documents_id_vault_id_key'::regclass::oid"
        )

        await _apply(conn)

        # Same index object, now owned by the constraint: the escape hatch for
        # a table too large to index inside the pool's statement timeout.
        assert await conn.fetchval(
            "SELECT 'documents_id_vault_id_key'::regclass::oid"
        ) == before
        assert await conn.fetchval(
            "SELECT conindid FROM pg_constraint WHERE conname = 'documents_id_vault_id_key'"
        ) == before


async def test_an_unrelated_index_wearing_the_name_fails_loudly():
    async with _fresh_database(undo_053=True) as conn:
        await conn.execute(
            "CREATE INDEX documents_id_vault_id_key ON documents (vault_id, path)"
        )
        with pytest.raises(RuntimeError, match="will not repurpose it"):
            await _apply(conn)


async def test_migration_022_does_not_drop_the_identity_column():
    """022 drops a column called document_id. 053 adds a different one, and
    init.sql declares it — so on a fresh database 022 runs against a table
    that already has one. It must leave it alone."""
    async with _fresh_database() as conn:
        await _load("022_publications_resource_uri.py").migrate(conn=conn)

        assert await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'publications' AND column_name = 'document_id'"
        ) == 1
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM pg_constraint WHERE conname = 'publications_document_fk'"
        ) == 1
