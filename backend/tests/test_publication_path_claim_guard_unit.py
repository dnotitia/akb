"""A write must refuse a path whose public link outlived its document.

`DocumentRepository.delete_with_publications` closes the ways new orphan
publications are *created*, but that is prospective only: it does nothing
about an orphan already sitting in a live database. Such a row is still
armed — publications resolve by path (migration 022 dropped the
`document_id` FK) and `documents` is `UNIQUE(vault_id, path)` — so the next
document to occupy that path is reached through the old public slug,
having never been published.

This file pins the other half of the fix: the three writes that can put a
document on a path refuse when a publication already occupies its canonical
URI, and refuse *nothing else*.

  - ordinary create   → `DocumentRepository.create`     (every REST/MCP/OKF/
                        agent-memory create funnels through `doc_service.put`)
  - move              → `DocumentService.move`, on the DESTINATION path
  - external-git sync → `DocumentRepository.upsert_external`, on the arm
                        that inserts a fresh row

**How the orphan is manufactured matters.** The product can no longer create
one, so these tests build the state by hand — but NOT by inserting a
hand-written `resource_uri`. A publication whose URI was assembled by the
test would agree with a guard whose URI is assembled the same wrong way, and
the pair would pass while production never matched anything. That is the
exact failure mode that produced this defect (see the
`delete_publications_for_document` docstring). So each test publishes a real
document through `create_publication_for_vault` — the production path, which
builds the URI from the resolved `documents.path` — and only then deletes the
document row directly, bypassing the chokepoint the way the pre-fix code did.
The URI under test is therefore genuinely the one production stores.

Path shapes are parametrised for the same reason: a URI builder that is
subtly wrong for vault-root documents, for nested collections, or for
non-ASCII names would leave the guard silently never firing for those.

Talks to a real Postgres via `AKB_TEST_DSN`; skips when unreachable, unless
`REQUIRE_REAL_PG=1` (set by the DB-backed CI job), where an unreachable
database fails instead of skipping so the gate cannot go quietly green. The
static tests at the bottom need no database and run everywhere.
"""

from __future__ import annotations

import ast
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from app.exceptions import ConflictError
from app.repositories.document_repo import DocumentRepository
from app.repositories.vault_repo import VaultRepository


_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:5433/akb",  # pragma: allowlist secret
)

_BACKEND = Path(__file__).resolve().parents[1]

# Vault root, one collection deep, nested, and a non-ASCII name. Each has a
# different `doc_uri` shape (`/doc/x` vs `/coll/…/doc/x`), and a builder that
# is wrong for one of them fails silently rather than loudly.
_PATH_SHAPES = [
    pytest.param("release-notes.md", id="vault-root"),
    pytest.param("reports/q3.md", id="one-collection-deep"),
    pytest.param("reports/2026/internal/q3.md", id="nested-collections"),
    pytest.param("보고서/한글-문서.md", id="unicode"),
]


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
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"REQUIRE_REAL_PG=1 but Postgres is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")
    pool = await asyncpg.create_pool(dsn=_DSN, min_size=2, max_size=10)
    init_sql = (_BACKEND / "app" / "db" / "init.sql").read_text()
    async with pool.acquire() as conn:
        await conn.execute(init_sql)
    # Services under test resolve their connection through the module-global
    # `get_pool()`; wire it to this pool for the duration of the test.
    from app.db import postgres as pg_mod
    prev = pg_mod._pool
    pg_mod._pool = pool
    try:
        # init.sql is only half the schema — `events`, `resource_aliases`,
        # `chunks.vault_id` and friends arrive by migration, and the writes
        # exercised here touch all of them. Drive the app's own runner rather
        # than a hand-copied DDL list that would rot on the next migration.
        await pg_mod._apply_migrations()
        yield pool
    finally:
        pg_mod._pool = prev
        await pool.close()


@pytest_asyncio.fixture
async def vault(pool):
    vault_repo = VaultRepository(pool)
    name = f"_orphan_guard_{uuid.uuid4().hex[:8]}"
    vid = await vault_repo.create(
        name=name,
        description="ephemeral unit-test vault",
        git_path=f"/tmp/{name}.git",
        owner_id=None,
    )
    try:
        yield {"id": vid, "name": name}
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vaults WHERE id = $1", vid)


@pytest.fixture(autouse=True)
def _public_base_url(monkeypatch):
    """`create_publication` renders a share URL, which refuses to build
    without a configured public base. Irrelevant to what is under test, but
    required to reach it."""
    from app.services import publication_service
    monkeypatch.setattr(
        publication_service.settings, "public_base_url",
        "https://orphan-guard.test.local", raising=False,
    )


# ── Manufacturing an orphan ──────────────────────────────────────


async def _create_doc(pool, vault, path: str, *, title: str = "Doc") -> uuid.UUID:
    """Create a document row through the real repository (guard included)."""
    doc_repo = DocumentRepository(pool)
    return await doc_repo.create(
        vault_id=vault["id"], collection_id=None, path=path, title=title,
        doc_type="note", status="draft", summary=None, domain=None,
        created_by=None, now=datetime.now(timezone.utc),
        commit_hash="c" * 40, content_hash="h", hash_algorithm="sha256",
        tags=[], metadata={}, vault_name=vault["name"],
    )


async def _publish(vault_name: str, doc_path: str) -> str:
    """Publish through the production entry point, so `resource_uri` is
    built exactly the way a real publication stores it."""
    from app.services.publication_service import create_publication_for_vault
    pub = await create_publication_for_vault(
        vault_name=vault_name, resource_type="document", doc_id=doc_path,
    )
    return pub["slug"]


async def _orphan_the_publication(pool, doc_id: uuid.UUID) -> None:
    """Delete the document row and leave its publication behind.

    Deliberately NOT `delete_with_publications` — this reproduces what the
    pre-fix delete paths did, which is the only way to reach the state the
    guard exists for now that the product refuses to produce it.
    """
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM documents WHERE id = $1", doc_id)


async def _armed_path(pool, vault, path: str) -> str:
    """Publish a document at `path`, then orphan it. Returns the slug."""
    doc_id = await _create_doc(pool, vault, path, title="Published original")
    slug = await _publish(vault["name"], path)
    await _orphan_the_publication(pool, doc_id)
    return slug


async def _doc_count(pool, vault, path: str) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT count(*) FROM documents WHERE vault_id = $1 AND path = $2",
            vault["id"], path,
        )


async def _publication_exists(pool, slug: str) -> bool:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT count(*) FROM publications WHERE slug = $1", slug,
        ) == 1


# ── 1. Ordinary create ───────────────────────────────────────────


@pytest.mark.parametrize("path", _PATH_SHAPES)
async def test_create_refuses_a_path_armed_by_an_orphan_publication(pool, vault, path):
    """The core case: a slug outlived its document, and a new document at
    that path would be reachable through it."""
    slug = await _armed_path(pool, vault, path)

    with pytest.raises(ConflictError) as excinfo:
        await _create_doc(pool, vault, path, title="Replacement")

    message = str(excinfo.value)
    assert slug in message, (
        "the error must name the publication to revoke — without the slug "
        f"there is nothing actionable in it: {message!r}"
    )
    assert path in message
    assert await _doc_count(pool, vault, path) == 0, "the write must not land"
    assert await _publication_exists(pool, slug), (
        "the stale row carries slug, creator and view count, which the owner "
        "needs in order to decide what to do with it — the "
        "guard must not clean it up"
    )


async def test_create_refuses_a_path_armed_by_a_pre_026_legacy_uri(pool, vault):
    """The guard matches stored URIs by equality, so every shape the RESOLVER
    still accepts has to be one of the strings it probes.

    `parse_uri` accepts the pre-0.3.0 nested form — `akb://V/doc/reports/q3.md`,
    the whole path in the identifier with no `/coll/` segment — and maps it to
    exactly the same document as the canonical form. Migration 026 rewrote
    every stored URI, so this shape should no longer exist; a row restored
    from an older backup, or one the migration missed, would still be served.
    The URI is hand-written here because nothing emits this shape any more —
    and the first assertion is the check on that hand-writing: if it did not
    resolve to (this vault, this path), the resolver could not serve it either
    and the test would be proving nothing.
    """
    path = "reports/q3.md"
    legacy_uri = f"akb://{vault['name']}/doc/{path}"
    from app.services.uri_service import parse_uri
    parsed = parse_uri(legacy_uri)
    assert parsed is not None and parsed.kind == "doc"
    assert (parsed.vault, parsed.identifier) == (vault["name"], path), (
        "premise of this test: the legacy shape resolves to the same document"
    )

    doc_id = await _create_doc(pool, vault, path, title="Published original")
    slug = await _publish(vault["name"], path)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE publications SET resource_uri = $1 WHERE slug = $2",
            legacy_uri, slug,
        )
    await _orphan_the_publication(pool, doc_id)

    with pytest.raises(ConflictError) as excinfo:
        await _create_doc(pool, vault, path, title="Replacement")
    assert slug in str(excinfo.value)
    assert await _doc_count(pool, vault, path) == 0


async def test_create_allows_a_path_with_no_publication(pool, vault):
    """The overwhelmingly common case. A guard that rejects too much is
    worse than none."""
    doc_id = await _create_doc(pool, vault, "reports/q3.md")
    assert doc_id is not None
    assert await _doc_count(pool, vault, "reports/q3.md") == 1


async def test_create_allows_a_path_whose_neighbour_is_published(pool, vault):
    """A live publication elsewhere in the vault — even in the same
    collection — says nothing about this path."""
    live = await _create_doc(pool, vault, "reports/q2.md", title="Published")
    slug = await _publish(vault["name"], "reports/q2.md")

    await _create_doc(pool, vault, "reports/q3.md", title="Sibling")

    assert await _doc_count(pool, vault, "reports/q3.md") == 1
    assert await _publication_exists(pool, slug)
    assert live is not None


async def test_create_over_a_live_published_document_is_still_a_path_collision(pool, vault):
    """A publication whose document is ALIVE is not an orphan, so the guard
    must stay out of the way and let the ordinary `UNIQUE(vault_id, path)`
    conflict surface with its own message. Getting this wrong would relabel
    a routine 409 as a security incident."""
    await _create_doc(pool, vault, "reports/q3.md", title="Published original")
    slug = await _publish(vault["name"], "reports/q3.md")

    with pytest.raises(ConflictError) as excinfo:
        await _create_doc(pool, vault, "reports/q3.md", title="Second")

    message = str(excinfo.value)
    assert "Document already exists at path" in message, (
        f"expected the plain path-collision error, got: {message!r}"
    )
    assert slug not in message
    assert await _doc_count(pool, vault, "reports/q3.md") == 1


async def test_create_allows_a_path_freed_by_a_proper_delete(pool, vault):
    """Delete-then-recreate at the same path is an ordinary operation, and
    a delete through the chokepoint takes the publication with it. Nothing
    is left to refuse."""
    doc_id = await _create_doc(pool, vault, "reports/q3.md", title="Published original")
    slug = await _publish(vault["name"], "reports/q3.md")

    doc_repo = DocumentRepository(pool)
    async with pool.acquire() as conn:
        async with conn.transaction():
            removed = await doc_repo.delete_with_publications(
                conn, doc_id=doc_id, vault_id=vault["id"],
            )
    assert removed is True
    assert not await _publication_exists(pool, slug)

    await _create_doc(pool, vault, "reports/q3.md", title="Recreated")
    assert await _doc_count(pool, vault, "reports/q3.md") == 1


# ── 2. Move ──────────────────────────────────────────────────────


class _StubGit:
    """Enough GitService for a move: records what it was asked to do so a
    refused write can be shown to have never reached git."""

    def __init__(self, body: str = "---\ntitle: Mover\n---\n\nBody text\n"):
        self.body = body
        self.moves: list[tuple[str, str]] = []

    def move_file(self, *, vault_name, old_path, new_path, message, author_name):
        self.moves.append((old_path, new_path))
        return "d" * 40

    def read_file(self, vault_name, path, commit=None):
        return self.body

    def current_commit(self, vault_name):
        return "d" * 40


def _document_service(git: _StubGit):
    from app.services.document_service import DocumentService
    return DocumentService(git=git)


async def test_move_refuses_a_destination_armed_by_an_orphan_publication(pool, vault):
    """`move` rewrites publications at the SOURCE uri so they follow the
    document; it has never looked at the destination. Moving an unpublished
    document onto an orphaned path hands it the orphan's public slug — the
    same outcome as a create, reached without creating anything."""
    slug = await _armed_path(pool, vault, "reports/q3.md")
    await _create_doc(pool, vault, "private/insider.md", title="Insider notes")

    git = _StubGit()
    with pytest.raises(ConflictError) as excinfo:
        await _document_service(git).move(
            vault["name"], "private/insider.md",
            collection="reports", slug="q3", agent_id="mover",
        )

    assert slug in str(excinfo.value)
    assert git.moves == [], (
        "the guard must run BEFORE the git mv — raising after it leaves git "
        "and PG disagreeing about where the document lives"
    )
    assert await _doc_count(pool, vault, "private/insider.md") == 1
    assert await _doc_count(pool, vault, "reports/q3.md") == 0
    assert await _publication_exists(pool, slug)


async def test_move_allows_a_clean_destination_and_carries_its_publication(pool, vault):
    """The ordinary move, including of a PUBLISHED document: it must still
    work, and its own publication must follow it to the new path."""
    await _create_doc(pool, vault, "drafts/plan.md", title="Plan")
    slug = await _publish(vault["name"], "drafts/plan.md")

    git = _StubGit()
    moved = await _document_service(git).move(
        vault["name"], "drafts/plan.md",
        collection="reports", slug="plan-final", agent_id="mover",
    )

    assert moved.path == "reports/plan-final.md"
    assert git.moves == [("drafts/plan.md", "reports/plan-final.md")]
    async with pool.acquire() as conn:
        uri = await conn.fetchval(
            "SELECT resource_uri FROM publications WHERE slug = $1", slug,
        )
    assert uri == f"akb://{vault['name']}/coll/reports/doc/plan-final.md"


async def test_move_to_a_path_freed_by_a_proper_delete_is_allowed(pool, vault):
    """Same negative as for create: a destination whose publication was
    cleaned up with its document is just an empty path."""
    doc_id = await _create_doc(pool, vault, "reports/q3.md", title="Published original")
    slug = await _publish(vault["name"], "reports/q3.md")
    doc_repo = DocumentRepository(pool)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await doc_repo.delete_with_publications(
                conn, doc_id=doc_id, vault_id=vault["id"],
            )
    assert not await _publication_exists(pool, slug)

    await _create_doc(pool, vault, "private/insider.md", title="Insider notes")
    git = _StubGit()
    moved = await _document_service(git).move(
        vault["name"], "private/insider.md",
        collection="reports", slug="q3", agent_id="mover",
    )
    assert moved.path == "reports/q3.md"


# ── 3. External-git import ───────────────────────────────────────


async def _upsert_external(pool, vault, path: str, *, blob: str = "blob1"):
    doc_repo = DocumentRepository(pool)
    return await doc_repo.upsert_external(
        vault_id=vault["id"], collection_id=None, path=path,
        external_path=path, external_blob=blob, title="Mirrored",
        doc_type="note", summary=None, domain=None, tags=[], metadata={},
        now=datetime.now(timezone.utc), commit_hash="e" * 40,
        content_hash="h", hash_algorithm="sha256", vault_name=vault["name"],
    )


@pytest.mark.parametrize("path", _PATH_SHAPES)
async def test_external_import_refuses_a_path_armed_by_an_orphan_publication(
    pool, vault, path,
):
    """An upstream commit that re-adds a path a previous sync deleted lands
    here as a fresh INSERT — the shape that re-points a surviving
    publication."""
    slug = await _armed_path(pool, vault, path)

    with pytest.raises(ConflictError) as excinfo:
        await _upsert_external(pool, vault, path)

    assert slug in str(excinfo.value)
    assert await _doc_count(pool, vault, path) == 0
    assert await _publication_exists(pool, slug)


async def test_external_import_of_a_free_path_is_allowed(pool, vault):
    doc_id, inserted = await _upsert_external(pool, vault, "mirror/readme.md")
    assert inserted is True
    assert doc_id is not None


async def test_a_path_with_no_parseable_uri_still_imports(pool, vault):
    """`parse_uri` rejects braces — they are template placeholders — so a
    mirrored upstream path like `{{cookiecutter.project}}/README.md` has no
    parseable canonical URI at all.

    A guard that insisted on parsing before checking would raise here, and in
    external-git a raised error holds the sync cursor: the whole mirror would
    stick permanently on a file that cannot be exposed anyway, since the
    resolver parses the stored URI too and 404s on it. String equality needs
    no parse, so both the create and the import go through.
    """
    weird = "{{cookiecutter.project}}/README.md"
    from app.services.uri_service import doc_uri, parse_uri
    assert parse_uri(doc_uri(vault["name"], weird)) is None, (
        "premise of this test: braces make the canonical URI unparseable"
    )

    doc_id, inserted = await _upsert_external(pool, vault, weird)
    assert inserted is True and doc_id is not None
    await _create_doc(pool, vault, "{braces}.md")
    assert await _doc_count(pool, vault, "{braces}.md") == 1


async def test_external_resync_of_a_published_mirror_is_not_blocked(pool, vault):
    """The ON CONFLICT arm: the document exists, so its publication is not an
    orphan and re-syncing it must go through. This is the case a guard
    written as 'is there a publication here?' would wrongly reject — every
    mirrored document that is also published would stop syncing."""
    doc_id, inserted = await _upsert_external(pool, vault, "mirror/readme.md")
    assert inserted is True
    slug = await _publish(vault["name"], "mirror/readme.md")

    same_id, inserted_again = await _upsert_external(
        pool, vault, "mirror/readme.md", blob="blob2",
    )
    assert inserted_again is False
    assert same_id == doc_id
    assert await _publication_exists(pool, slug)
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT external_blob FROM documents WHERE id = $1", doc_id,
        ) == "blob2"


# ── 4. Static gates (no database) ────────────────────────────────
#
# The guard is only as good as its coverage: a fourth way to put a row into
# `documents` would leave it intact but useless, and `move` refusing only
# after its `git mv` would leave git and PG disagreeing. Both are cheap to
# pin statically. (`put` deliberately commits to git BEFORE the guarded
# create — see `DocumentRepository.create` — so there is no such ordering to
# pin there.)

# Reuses the source enumeration from the delete-chokepoint gate rather than
# growing a second copy of it — the two files enforce opposite halves of the
# same invariant and must agree on what counts as production source.
from tests.test_document_delete_chokepoint_unit import _python_files  # noqa: E402


_GUARD = "assert_no_orphan_publication_for_document"

# Every production site allowed to INSERT a `documents` row, and why it is
# safe. Both are repository methods that call the guard first; anything else
# would be a path-claiming write that skips it.
_INSERT_ALLOWLIST = {
    ("app/repositories/document_repo.py", "create"): (
        "Ordinary create. Calls the guard on the same connection immediately "
        "before the INSERT; every REST/MCP/OKF/agent-memory create reaches it "
        "through document_service.put."
    ),
    ("app/repositories/document_repo.py", "upsert_external"): (
        "External-git mirror upsert. Same guard, correct for both arms: the "
        "orphan test is false by construction when the ON CONFLICT (update) "
        "arm will run."
    ),
}


def _outermost_function(tree: ast.AST, lineno: int) -> str:
    """The top-level method containing `lineno`, not the innermost closure.

    `create` issues its INSERT from a nested `_insert(c)` helper (so the same
    body can run on the caller's connection or a freshly acquired one), and an
    allowlist keyed on `_insert` would name an implementation detail that a
    rename could silently retire.
    """
    best, best_line = "<module>", None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = node.end_lineno or node.lineno
            if node.lineno <= lineno <= end and (best_line is None or node.lineno < best_line):
                best, best_line = node.name, node.lineno
    return best


def _insert_sites() -> dict[tuple[str, str], list[int]]:
    sites: dict[tuple[str, str], list[int]] = {}
    for path in _python_files():
        source = path.read_text()
        if "INSERT INTO documents" not in source:
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if "INSERT INTO documents" not in node.value:
                continue
            key = (
                path.relative_to(_BACKEND).as_posix(),
                _outermost_function(tree, node.lineno),
            )
            sites.setdefault(key, []).append(node.lineno)
    return sites


def _method_source(relpath: str, name: str) -> str:
    tree = ast.parse((_BACKEND / relpath).read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                body = body[1:]  # docstrings describe the guard too
            return "\n".join(ast.unparse(stmt) for stmt in body)
    pytest.fail(f"{name} not found in {relpath}")


def test_every_documents_insert_runs_the_orphan_guard():
    """A new way to create a `documents` row fails here. Rows are claimed by
    path, and an unguarded claim is what lets that happen.

    That the guard runs BEFORE the row lands is asserted behaviourally
    instead of lexically (the refused writes above leave no row): `create`
    and `upsert_external` both issue their SQL from a nested runner defined
    after the statement text, so source order says nothing useful about
    execution order here.
    """
    found = _insert_sites()
    unlisted = sorted(set(found) - set(_INSERT_ALLOWLIST))
    assert not unlisted, (
        f"unallowlisted document INSERT: {unlisted}. A write that claims a "
        f"path must call {_GUARD}() on the same connection first, or a "
        "publication that outlived its document re-points at it."
    )
    stale = sorted(set(_INSERT_ALLOWLIST) - set(found))
    assert not stale, f"_INSERT_ALLOWLIST entries with no matching site: {stale}"

    missing = [
        func for _, func in sorted(_INSERT_ALLOWLIST)
        if _GUARD not in _method_source("app/repositories/document_repo.py", func)
    ]
    assert not missing, f"allowlisted insert site no longer calls {_GUARD}(): {missing}"


def test_move_runs_the_guard_before_it_touches_git():
    """git is not transactional. A move that commits the `git mv` and then
    rejects leaves git and PG disagreeing about where the document lives —
    a worse state than the write being refused."""
    body = _method_source("app/services/document_service.py", "move")
    guard = body.find(_GUARD + "(")
    git_write = body.find("self.git.move_file")
    assert guard != -1, f"move() must call {_GUARD}()"
    assert git_write != -1
    assert guard < git_write, (
        f"move() must run the guard before the git mv (guard@{guard}, "
        f"git@{git_write})"
    )
