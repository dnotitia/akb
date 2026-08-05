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


async def _apply(conn) -> dict:
    """Apply the migration, returning the backfill's per-category counts."""
    return await _load("053_publication_document_identity.py").migrate(conn=conn)


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
    assert any(
        "idx_publications_document_id" in definition
        and "(document_id, vault_id) WHERE (document_id IS NOT NULL)" in definition
        for _, definition, _ in expected["indexes"]
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


# Every column named by a STANDALONE statement in init.sql that some
# migration adds. A database that predates the migration has the table but not
# the column, and init.sql runs first, so each of these needs a guard or the
# boot aborts before migrations can fix anything.
_MIGRATION_ADDED_COLUMNS = [
    ("publications", "document_id", "idx_publications_document_id"),   # 053
    ("publications", "resource_uri", "idx_publications_resource_uri"),  # 022
    ("chunks", "vector_indexed_at", "idx_chunks_indexing_queue"),       # 009
    ("vault_tables", "collection_id", "idx_vault_tables_collection"),   # 020
    ("vault_files", "collection_id", "idx_vault_files_collection"),     # 020
]


@pytest.mark.parametrize("table,column,index", _MIGRATION_ADDED_COLUMNS)
async def test_init_sql_survives_a_database_missing_a_migration_added_column(
    table, column, index,
):
    """The boot-loop class, as a rule rather than one instance of it.

    All but the first are dead in practice — their migrations are applied
    everywhere — and they are checked anyway, because the cost of the rule
    being unevenly applied is that the next person reads the guarded
    statements as optional.
    """
    async with _fresh_database() as conn:
        await conn.execute(f"ALTER TABLE {table} DROP COLUMN {column} CASCADE")

        await conn.execute(_INIT_SQL)  # must not raise

        assert await conn.fetchval(f"SELECT to_regclass('public.{index}')") is None


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


async def test_the_backfill_reports_a_count_for_every_category(caplog):
    """The per-category counts are a contract, not debug output.

    They are what an operator reads to decide whether a backfill can be
    trusted, so a row landing in the wrong bucket is a real defect even when
    the end state is right — "48 bound, 1 path reused" and "48 bound, 1 vault
    mismatch" describe very different databases and would prompt very
    different decisions. Asserting only that unbindable rows end up NULL
    cannot tell those apart.
    """
    async with _fresh_database(undo_053=True) as conn:
        own = f"counts-{uuid.uuid4().hex[:8]}"
        elsewhere = f"counts-other-{uuid.uuid4().hex[:8]}"
        vault = await _vault(conn, own)
        other_vault = await _vault(conn, elsewhere)

        # bound
        await _document(conn, vault, "keep.md")
        await _publication(conn, vault, f"akb://{own}/doc/keep.md")
        # no_document
        await _publication(conn, vault, f"akb://{own}/doc/gone.md")
        # path_reused — document postdates the publication
        older = await conn.fetchval("SELECT NOW() - INTERVAL '1 hour'")
        await _publication(conn, vault, f"akb://{own}/doc/reused.md", created_at=older)
        await _document(conn, vault, "reused.md")
        # vault_mismatch — with a same-path document in the row's OWN vault, so
        # only the vault-name comparison can refuse it
        await _document(conn, vault, "both.md")
        await _document(conn, other_vault, "both.md")
        await _publication(conn, vault, f"akb://{elsewhere}/doc/both.md")
        # unreadable_uri
        await _publication(conn, vault, f"akb://{own}/doc/{{template}}.md")
        # non_document_publications
        await _publication(conn, vault, None, resource_type="table_query")
        await _publication(
            conn, vault, f"akb://{own}/file/{uuid.uuid4()}", resource_type="file"
        )

        with caplog.at_level("INFO", logger="akb.migration.053"):
            first = await _apply(conn)

        assert first == {
            "examined": 5,
            "bound": 1,
            "no_document": 1,
            "path_reused": 1,
            "unreadable_uri": 1,
            "vault_mismatch": 1,
            "changed_underfoot": 0,
            "already_bound": 0,
            "non_document_publications": 2,
        }
        # The log an operator reads carries the same figures as the return
        # value a test can assert on — otherwise only one of them is checked.
        summary = next(
            r.getMessage() for r in caplog.records if "backfill:" in r.getMessage()
        )
        for key, value in first.items():
            if key == "examined":
                continue
            assert f"{key}={value}" in summary, f"{key} missing from {summary!r}"

        # Re-run: the bound row moves into already_bound and is not counted
        # again, and every refusal is reported identically.
        second = await _apply(conn)
        assert second == {**first, "examined": 4, "bound": 0, "already_bound": 1}


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


async def test_a_constraint_that_only_borrowed_the_name_is_not_accepted():
    """Idempotency is not "something by that name exists". Index and constraint
    names are unique per schema, not per table, so a name says nothing about
    what is behind it."""
    # A CHECK constraint wearing the FK's name.
    async with _fresh_database(undo_053=True) as conn:
        await conn.execute(
            "ALTER TABLE publications ADD COLUMN document_id UUID, "
            "ADD CONSTRAINT publications_document_fk CHECK (view_count >= 0)"
        )
        with pytest.raises(RuntimeError, match="not the one migration 053 installs"):
            await _apply(conn)

    # An FK of the right name on the right table, but single-column — the
    # shape this whole change exists to replace.
    async with _fresh_database(undo_053=True) as conn:
        await conn.execute(
            "ALTER TABLE publications ADD COLUMN document_id UUID, "
            "ADD CONSTRAINT publications_document_fk "
            "FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE"
        )
        with pytest.raises(RuntimeError, match="not the one migration 053 installs"):
            await _apply(conn)

    # An index of the right name and columns, but on another table.
    async with _fresh_database(undo_053=True) as conn:
        await conn.execute(
            "CREATE TABLE decoy (document_id UUID, vault_id UUID); "
            "CREATE INDEX idx_publications_document_id "
            "ON decoy (document_id, vault_id) WHERE document_id IS NOT NULL"
        )
        with pytest.raises(RuntimeError, match="not the cascade index"):
            await _apply(conn)

    # Right name, right table, right columns — but UNIQUE, which would let a
    # document be published exactly once.
    async with _fresh_database(undo_053=True) as conn:
        await conn.execute("ALTER TABLE publications ADD COLUMN document_id UUID")
        await conn.execute(
            "CREATE UNIQUE INDEX idx_publications_document_id "
            "ON publications (document_id, vault_id) WHERE document_id IS NOT NULL"
        )
        with pytest.raises(RuntimeError, match="not the cascade index"):
            await _apply(conn)


async def test_an_invalid_cascade_index_is_rebuilt_not_stepped_over():
    async with _fresh_database(undo_053=True) as conn:
        await conn.execute("ALTER TABLE publications ADD COLUMN document_id UUID")
        await conn.execute(
            "CREATE INDEX idx_publications_document_id "
            "ON publications (document_id, vault_id) WHERE document_id IS NOT NULL"
        )
        await conn.execute(
            "UPDATE pg_index SET indisvalid = false "
            "WHERE indexrelid = 'idx_publications_document_id'::regclass"
        )

        await _apply(conn)

        assert await conn.fetchval(
            "SELECT indisvalid FROM pg_index "
            "WHERE indexrelid = 'idx_publications_document_id'::regclass"
        ) is True


async def test_a_pre_existing_bad_binding_stops_the_migration_without_touching_it():
    """If document_id already holds a value the composite key would reject, the
    migration fails loudly and changes nothing. Blanking or deleting those rows
    is a decision for a human, not a side effect of a boot."""
    async with _fresh_database(undo_053=True) as conn:
        vault_a = await _vault(conn, f"bad-a-{uuid.uuid4().hex[:8]}")
        vault_b = await _vault(conn, f"bad-b-{uuid.uuid4().hex[:8]}")
        other_doc = await _document(conn, vault_b, "notes.md")
        await conn.execute("ALTER TABLE publications ADD COLUMN document_id UUID")
        pub = await _publication(conn, vault_a, "akb://x/doc/notes.md")
        await conn.execute(
            "UPDATE publications SET document_id = $2 WHERE id = $1", pub, other_doc
        )

        with pytest.raises(RuntimeError, match="does not name a document"):
            await _apply(conn)

        # The row is untouched and the connection is still usable.
        assert await conn.fetchval(
            "SELECT document_id FROM publications WHERE id = $1", pub
        ) == other_doc
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM pg_constraint WHERE conname = 'publications_document_fk'"
        ) == 0


async def test_the_backfill_pages_through_more_rows_than_one_batch():
    """_BATCH is 500; the paging loop has to visit everything, exactly once.

    Including the row whose id is all zeroes. Nothing generates that id, but
    the column accepts it, and a cursor that started from it with a strict `>`
    would skip exactly that row — silently, since every other row still binds.
    """
    module = _load("053_publication_document_identity.py")
    total = module._BATCH * 2 + 7
    zero_id = uuid.UUID(int=0)

    async with _fresh_database(undo_053=True) as conn:
        name = f"paged-{uuid.uuid4().hex[:8]}"
        vault = await _vault(conn, name)
        await conn.execute(
            "INSERT INTO documents (vault_id, path, title) "
            "SELECT $1, 'p/' || g || '.md', 'doc' FROM generate_series(1, $2) g",
            vault, total,
        )
        await conn.execute(
            "INSERT INTO publications (slug, vault_id, resource_type, resource_uri) "
            "SELECT 'slug-' || g, $1, 'document', $2 || '/coll/p/doc/' || g || '.md' "
            "  FROM generate_series(1, $3) g",
            vault, f"akb://{name}", total,
        )
        # The lowest possible key, sorting before every generated one.
        zero_doc = await _document(conn, vault, "zero.md")
        await conn.execute(
            "INSERT INTO publications (id, slug, vault_id, resource_type, resource_uri) "
            "VALUES ($1, 'slug-zero', $2, 'document', $3)",
            zero_id, vault, f"akb://{name}/doc/zero.md",
        )

        await _apply(conn)

        assert await conn.fetchval(
            "SELECT document_id FROM publications WHERE id = $1", zero_id
        ) == zero_doc
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM publications WHERE document_id IS NULL"
        ) == 0
        # Each publication bound to its OWN document, not to some other page's.
        # (The root-level `zero.md` row is asserted separately above; this
        # covers the generated in-collection ones.)
        assert await conn.fetchval(
            """
            SELECT COUNT(*) FROM publications p JOIN documents d ON d.id = p.document_id
             WHERE d.path LIKE 'p/%'
               AND p.resource_uri <> 'akb://' || $1 || '/coll/p/doc/' ||
                   split_part(d.path, '/', 2)
            """,
            name,
        ) == 0
        assert await conn.fetchval(
            "SELECT COUNT(DISTINCT document_id) FROM publications"
        ) == total + 1


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
