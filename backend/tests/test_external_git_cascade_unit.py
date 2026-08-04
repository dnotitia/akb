"""The external-git publication cascade, and the two invariants that keep it
out of reach.

`external_git_service._delete_external_path` deletes a mirrored document
through `DocumentRepository.delete_with_publications`, so an upstream commit
that removes a path takes the document's publications with it. Without that,
the surviving publication would resolve by path — and an upstream *revert*
re-adding the path (or a rename back) would republish whatever landed there,
through the old public slug.

**That cascade cannot currently fire through the product**, because nothing can
publish a mirrored document in the first place. Two invariants stand in the
way, and neither is written down anywhere the code can enforce:

  1. A mirror vault refuses `required_role="writer"` for every user including
     the owner (`access_service.py:220-231`), and publishing asks for exactly
     that role on both surfaces.
  2. `vault_external_git` has exactly one INSERT path, inside `create_vault`'s
     transaction (`document_service.py:1756`), so a vault is a mirror from
     birth — and a mirror is empty at birth, so no document that predates the
     mirror flag can carry a publication into it.

This file is therefore NOT a red-then-green regression test: the cascade is
already correct. It is a guard on all three facts at once. The day someone
adds "attach an external-git remote to an existing vault", or relaxes the
mirror guard, or moves publishing to a different role, invariant 1 or 2 fails
in review — and the cascade test is what says the remaining protection still
works.

The cascade test drives the REAL git binary over `tests/extgit_http`'s
in-process smart-HTTP server (a fake `mirror.test` host pinned to 127.0.0.1
through the hermetic runner's DNS pin), against a REAL PostgreSQL. Nothing
about the delete path is mocked: the upstream commit, the fetch, the tree
diff, the row lock, the publication cleanup and the event are all the
production code.

Talks to Postgres via `AKB_TEST_DSN`; skips when unreachable, unless
`REQUIRE_REAL_PG=1` (the DB-backed CI job), where an unreachable database
fails rather than skips so the gate cannot go quietly green. The invariant
tests at the bottom are static and run everywhere.
"""

from __future__ import annotations

import ast
import os
import re
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from tests.source_scan import BACKEND as _BACKEND, enclosing_function, python_files

from app.exceptions import ForbiddenError
from app.repositories.vault_external_git_repo import VaultExternalGitRepository
from app.repositories.vault_repo import VaultRepository
from app.services.external_git_service import ExternalGitService
from app.services.git_service import GitService
from app.services.uri_service import doc_uri
from tests.extgit_http import build_runner


_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:5433/akb",  # pragma: allowlist secret
)


_MIRRORED_PATH = "reports/q3.md"
_MIRRORED_BODY = "# Q3\n\nUpstream content that a public link points at.\n"


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
    async with pool.acquire() as conn:
        await conn.execute((_BACKEND / "app" / "db" / "init.sql").read_text())
    from app.db import postgres as pg_mod
    prev = pg_mod._pool
    pg_mod._pool = pool
    try:
        # `reconcile` writes events, chunks and the mirror sidecar's scheduler
        # columns — all of which arrive by migration, not init.sql.
        await pg_mod._apply_migrations()
        yield pool
    finally:
        pg_mod._pool = prev
        await pool.close()


@pytest.fixture(autouse=True)
def _public_base_url(monkeypatch):
    """`create_publication` renders a share URL and refuses to build one
    without a configured public base."""
    from app.services import publication_service
    monkeypatch.setattr(
        publication_service.settings, "public_base_url",
        "https://cascade.test.local", raising=False,
    )


@pytest_asyncio.fixture
async def created(pool):
    """Rows to remove on teardown. Vaults go first: the vault-scoped tables
    this file writes (`documents`, `publications`, `vault_external_git`,
    `chunks`) are all `ON DELETE CASCADE` on `vaults.id`, but `vaults.owner_id`
    references `users`, so a user cannot be removed while it still owns one.

    `events` is deliberately left behind: its `vault_id` carries no foreign key
    (`migrations/015_events_outbox.py:50`) because the outbox must survive the
    vault it describes long enough to be published. A few rows per run in a
    scratch database, and removing them would be this test asserting a
    retention policy it does not own.
    """
    ids: dict[str, list] = {"vaults": [], "users": []}
    try:
        yield ids
    finally:
        async with pool.acquire() as conn:
            for vault_id in ids["vaults"]:
                await conn.execute("DELETE FROM vaults WHERE id = $1", vault_id)
            for user_id in ids["users"]:
                await conn.execute("DELETE FROM users WHERE id = $1", user_id)


# ── 1. The cascade, end to end ───────────────────────────────────


class _Mirror:
    """A live mirror vault: upstream repo, DB rows, and the service wired to
    the fixture's git transport."""

    def __init__(self, *, vault_id, name, url, svc, git, fixture, repo_name):
        self.vault_id = vault_id
        self.name = name
        self.url = url
        self.svc = svc
        self.git = git
        self.fixture = fixture
        self.repo_name = repo_name


async def _make_mirror(pool, git_http, tmp_path, created, *, files: dict[str, str]) -> _Mirror:
    """Create the upstream repo, the vault + sidecar rows, and activate the
    sidecar so `mark_success` can advance the cursor.

    The sidecar is born `sync_state='pending_preflight'` and the poller's
    Layer-2 preflight is what promotes it; this calls the same repository
    method that preflight does rather than writing `sync_state` by hand.
    """
    repo_name = f"cascade{uuid.uuid4().hex[:8]}"
    url, _head = git_http.add_repo(repo_name, files)

    git = GitService(
        storage_path=str(tmp_path / "vaults"),
        ext_runner=build_runner(git_http.port),
    )
    name = f"mirror_{uuid.uuid4().hex[:8]}"
    vault_id = await VaultRepository(pool).create(
        name=name,
        description="external-git cascade fixture",
        git_path=str(git._bare_path(name)),
        owner_id=None,
    )
    created["vaults"].append(vault_id)
    ext_repo = VaultExternalGitRepository(pool)
    await ext_repo.create(
        vault_id=vault_id, remote_url=url, remote_branch="main",
        auth_token=None, poll_interval_secs=60,
    )
    assert await ext_repo.activate_from_preflight(vault_id, url, None, 60) is True

    return _Mirror(
        vault_id=vault_id, name=name, url=url,
        svc=ExternalGitService(git=git), git=git,
        fixture=git_http, repo_name=repo_name,
    )


async def _doc_row(pool, vault_id, path: str) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, path, source, external_path FROM documents "
            " WHERE vault_id = $1 AND path = $2",
            vault_id, path,
        )
    return dict(row) if row else None


async def _publication_row(pool, slug: str) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT slug, resource_uri, vault_id FROM publications WHERE slug = $1",
            slug,
        )
    return dict(row) if row else None


async def _publish_mirrored(mirror, path: str) -> str:
    """Publish a mirrored document.

    Deliberately `create_publication` and not the REST/MCP surface: publishing
    asks for `required_role="writer"`, which a mirror vault refuses for every
    user (invariant 1, pinned below). The service function has no mirror check,
    which is exactly why the cascade has to be correct anyway — this is the one
    seam through which a mirror publication could ever exist.
    """
    from app.services.publication_service import create_publication
    pub = await create_publication(
        vault_id=mirror.vault_id,
        resource_type="document",
        resource_uri=doc_uri(mirror.name, path),
        title="Q3 (public)",
    )
    return pub["slug"]


@pytest.fixture
def mirror_doc_service(monkeypatch, tmp_path, git_http):
    """Point the publication resolver's cached DocumentService at the fixture's
    git storage, so resolving a mirror publication reads the real mirrored body
    instead of falling back to 'content unavailable' against the default
    storage root."""
    from app.services import publication_service
    from app.services.document_service import DocumentService
    monkeypatch.setattr(
        publication_service, "_doc_service",
        DocumentService(git=GitService(
            storage_path=str(tmp_path / "vaults"),
            ext_runner=build_runner(git_http.port),
        )),
        raising=False,
    )


async def test_upstream_delete_takes_the_publication_with_the_document(
    pool, git_http, tmp_path, created, mirror_doc_service,
):
    """An upstream commit that removes a mirrored path must remove that
    document's publications in the same transaction.

    Every step is real: `git rm` + push to the served bare, a fetch through the
    hermetic runner over HTTP, the tree diff, and the tombstone's route through
    `DocumentRepository.delete_with_publications`.
    """
    from app.services.publication_service import (
        PublicationNotFound,
        get_publication_by_slug,
        resolve_document_publication,
        resolve_publication,
    )

    mirror = await _make_mirror(
        pool, git_http, tmp_path, created, files={_MIRRORED_PATH: _MIRRORED_BODY},
    )

    served_before = git_http.request_count
    first = await mirror.svc.reconcile(mirror.vault_id, mirror.name)
    assert first["status"] == "synced", first
    assert first["added"] == 1, first
    # The fixture counts every HTTP request it serves. Zero would mean the
    # "mirror" was materialised without touching the transport — a local copy,
    # a cached bare, a stubbed runner — and every assertion below would be
    # about plumbing this test built rather than about the product.
    assert git_http.request_count > served_before, (
        "the reconcile performed no git traffic against the fixture server"
    )
    indexed = await _doc_row(pool, mirror.vault_id, _MIRRORED_PATH)
    assert indexed is not None
    assert (indexed["source"], indexed["external_path"]) == ("external_git", _MIRRORED_PATH), (
        f"the row at {_MIRRORED_PATH} did not come from the mirror import: {indexed}"
    )

    slug = await _publish_mirrored(mirror, _MIRRORED_PATH)

    # The link is genuinely live before the delete — if it were not, the
    # assertions after the delete would prove nothing.
    live = await resolve_publication(slug=slug)
    assert live["slug"] == slug
    resolved = await resolve_document_publication(live)
    assert resolved["content_unavailable"] is False, resolved
    assert "Upstream content" in resolved["content"]

    upstream_head = mirror.fixture.remove_path(mirror.repo_name, _MIRRORED_PATH)
    served_before_delete = git_http.request_count
    second = await mirror.svc.reconcile(mirror.vault_id, mirror.name)

    assert second["status"] == "synced", second
    # The delete leg is pinned to the transport and to the exact upstream
    # commit — not merely to "some reconcile ran". A cached or short-circuited
    # fetch would leave the count flat or the sha stale, and the deletion
    # assertions below would be describing a tree nobody fetched.
    assert git_http.request_count > served_before_delete, (
        "the deleting reconcile performed no git traffic"
    )
    assert second["sha"] == upstream_head, (
        f"reconciled against {second['sha']}, not the commit that removed the "
        f"path ({upstream_head})"
    )
    assert second["deleted"] == 1, second
    assert await _doc_row(pool, mirror.vault_id, _MIRRORED_PATH) is None
    assert await _publication_row(pool, slug) is None, (
        "the publication outlived the document it pointed at — a slug that "
        "still resolves by path, waiting for the next document at that path"
    )
    assert await get_publication_by_slug(slug) is None
    with pytest.raises(PublicationNotFound):
        await resolve_publication(slug=slug)


async def test_an_upstream_revert_cannot_resurrect_the_old_slug(
    pool, git_http, tmp_path, created,
):
    """The shape of the exposure the cascade exists to prevent.

    Upstream removes a published path and then adds it back — an ordinary
    revert, or a rename and a rename back. The re-added file is indexed as a
    NEW document (fresh id) at the same path, and no slug is left pointing at
    it.

    What this does NOT do is demonstrate the anonymous read: with the cascade
    broken, the re-import never lands at all, because
    `DocumentRepository.upsert_external` now refuses to write onto a path a
    publication still claims (the orphan-write guard). The two layers compose
    — the cascade prevents the orphan, and the guard refuses to arm one that
    exists anyway — so this test fails on the surviving publication row rather
    than on a leaked body. That is the honest account of what it proves.
    """
    mirror = await _make_mirror(
        pool, git_http, tmp_path, created, files={_MIRRORED_PATH: _MIRRORED_BODY},
    )
    first = await mirror.svc.reconcile(mirror.vault_id, mirror.name)
    assert first["added"] == 1, first
    before = await _doc_row(pool, mirror.vault_id, _MIRRORED_PATH)
    assert before is not None
    slug = await _publish_mirrored(mirror, _MIRRORED_PATH)

    mirror.fixture.remove_path(mirror.repo_name, _MIRRORED_PATH)
    second = await mirror.svc.reconcile(mirror.vault_id, mirror.name)
    assert second["deleted"] == 1, second
    assert await _doc_row(pool, mirror.vault_id, _MIRRORED_PATH) is None

    mirror.fixture.publish_change(
        mirror.repo_name, _MIRRORED_PATH,
        "# Q3 INTERNAL\n\nDifferent content.\n",
    )
    third = await mirror.svc.reconcile(mirror.vault_id, mirror.name)
    assert third["added"] == 1, third

    after = await _doc_row(pool, mirror.vault_id, _MIRRORED_PATH)
    assert after is not None and after["id"] != before["id"], (
        "premise: the revert must produce a NEW document row at the reused path"
    )
    assert await _publication_row(pool, slug) is None, (
        f"slug {slug} survived and now points at a different document"
    )


async def test_an_untouched_publication_survives_an_unrelated_upstream_delete(
    pool, git_http, tmp_path, created,
):
    """The cascade must be scoped to the path that disappeared. A guard that
    cleaned too much would take live publications down with it, which is a
    silent availability bug rather than a loud one."""
    other = "reports/q2.md"
    mirror = await _make_mirror(
        pool, git_http, tmp_path, created,
        files={_MIRRORED_PATH: _MIRRORED_BODY, other: "# Q2\n\nStill here.\n"},
    )
    await mirror.svc.reconcile(mirror.vault_id, mirror.name)
    keep_slug = await _publish_mirrored(mirror, other)
    drop_slug = await _publish_mirrored(mirror, _MIRRORED_PATH)

    mirror.fixture.remove_path(mirror.repo_name, _MIRRORED_PATH)
    result = await mirror.svc.reconcile(mirror.vault_id, mirror.name)

    assert result["deleted"] == 1, result
    assert await _publication_row(pool, drop_slug) is None
    assert await _publication_row(pool, keep_slug) is not None, (
        "an unrelated publication was collateral damage"
    )
    assert await _doc_row(pool, mirror.vault_id, other) is not None


# ── 2. Invariant 1: a mirror vault is read-only to writers ───────


async def _user(pool, created, *, is_admin: bool = False) -> uuid.UUID:
    async with pool.acquire() as conn:
        user_id = await conn.fetchval(
            "INSERT INTO users (username, email, password_hash, is_admin) "
            "VALUES ($1, $2, 'x', $3) RETURNING id",
            f"u_{uuid.uuid4().hex[:10]}", f"{uuid.uuid4().hex[:10]}@test.local",
            is_admin,
        )
    created["users"].append(user_id)
    return user_id


async def _plain_vault(pool, created, owner_id) -> str:
    name = f"plain_{uuid.uuid4().hex[:8]}"
    vault_id = await VaultRepository(pool).create(
        name=name, description="control", git_path=f"/tmp/{name}.git",
        owner_id=owner_id,
    )
    created["vaults"].append(vault_id)
    return name


async def _mirror_vault(pool, created, owner_id) -> str:
    name = f"mirror_{uuid.uuid4().hex[:8]}"
    vault_id = await VaultRepository(pool).create(
        name=name, description="mirror", git_path=f"/tmp/{name}.git",
        owner_id=owner_id,
    )
    created["vaults"].append(vault_id)
    await VaultExternalGitRepository(pool).create(
        vault_id=vault_id, remote_url="https://example.invalid/r.git",
        remote_branch="main", auth_token=None, poll_interval_secs=60,
    )
    return name


async def test_mirror_vault_refuses_writer_for_owner_and_for_system_admin(pool, created):
    """Invariant 1. The mirror guard sits BEFORE the owner and system-admin
    short-circuits, so neither can write — which is what stops any publication
    from ever existing in a mirror vault, and therefore what keeps the delete
    cascade unreachable through the product.

    Scope note: the guard tests `required_role == "writer"` only. `"admin"`
    (create_table / alter_table / drop_table) is NOT refused on a mirror —
    a known gap tracked separately. This test deliberately does not assert
    that gap's current behaviour in either direction; it pins the property
    publishing depends on.
    """
    from app.services.access_service import check_vault_access

    owner = await _user(pool, created)
    admin = await _user(pool, created, is_admin=True)
    vault = await _mirror_vault(pool, created, owner)

    for actor, who in ((owner, "owner"), (admin, "system admin")):
        with pytest.raises(ForbiddenError) as excinfo:
            await check_vault_access(str(actor), vault, required_role="writer")
        assert "read-only external git mirror" in str(excinfo.value), (
            f"{who} was refused for the wrong reason: {excinfo.value}"
        )


async def test_mirror_vault_still_allows_readers(pool, created):
    """The same guard must not break the point of a mirror. If reads were
    refused too, the test above would pass for a reason that has nothing to do
    with the invariant."""
    from app.services.access_service import check_vault_access

    owner = await _user(pool, created)
    vault = await _mirror_vault(pool, created, owner)
    access = await check_vault_access(str(owner), vault, required_role="reader")
    assert access["role"] == "owner"


async def test_the_same_vault_without_the_sidecar_allows_writer(pool, created):
    """Control: the refusal above comes from the `vault_external_git` row and
    nothing else."""
    from app.services.access_service import check_vault_access

    owner = await _user(pool, created)
    vault = await _plain_vault(pool, created, owner)
    access = await check_vault_access(str(owner), vault, required_role="writer")
    assert access["role"] == "owner"


_PUBLISH_SURFACES = {
    "app/api/routes/public.py": "create_publication_route",
    "mcp_server/server.py": "_handle_publish",
}


def test_publishing_has_exactly_the_two_entry_points_that_check_access():
    """Invariant 1 covers publishing only as far as the callers it can see.

    `create_publication` performs NO vault-access check of its own — the two
    surfaces below do it before delegating (via `create_publication_for_vault`).
    A third caller anywhere in the tree — an auto-publish feature, a seeder, a
    background job — would create publications without ever consulting the
    mirror guard, and every other test in this file would still pass while
    mirrored documents became publishable.
    """
    callers: dict[tuple[str, str], list[int]] = {}
    for path in _production_sources():
        source = path.read_text()
        if "create_publication" not in source:
            continue
        tree = ast.parse(source)
        rel = path.relative_to(_BACKEND).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else (
                node.func.id if isinstance(node.func, ast.Name) else None
            )
            if name in ("create_publication", "create_publication_for_vault"):
                # Keyed by (file, ENCLOSING FUNCTION), not by file: a third
                # route added to `public.py`, or a third handler added to
                # `server.py`, would otherwise ride in on the entry that
                # blessed the first one.
                key = (rel, enclosing_function(tree, node.lineno))
                callers.setdefault(key, []).append(node.lineno)

    # `create_publication_for_vault` delegating to `create_publication` is the
    # definition side, not a new surface.
    expected = set(_PUBLISH_SURFACES.items()) | {
        ("app/services/publication_service.py", "create_publication_for_vault"),
    }
    assert set(callers) == expected, (
        f"publication creation callers changed: {sorted(callers)}. Read this "
        "test's docstring — a new caller must do its own vault-access check "
        "with required_role='writer', or mirror vaults become publishable."
    )
    grew = {k: v for k, v in callers.items() if len(v) != 1}
    assert not grew, (
        f"a blessed publish entry point gained a second publication call: {grew}"
    )


def test_publishing_asks_for_the_role_the_mirror_guard_refuses():
    """The link in the chain nobody would think to check.

    Invariant 1 protects publishing only because publishing asks for
    `writer` — the one role the mirror guard refuses. Moving either publish
    surface to `admin` (a role the guard does NOT cover) would silently make
    mirror publications creatable through the product, and every test above
    would still pass.

    EVERY `check_vault_access` call in each surface is asserted, not merely
    the presence of the string somewhere in the body. Both surfaces contain
    two: the publication's own vault, and (for `table_query`) each vault the
    query reads. A substring check would go green when only the FIRST was
    relaxed — the one that actually gates publishing a mirrored document —
    because the second would still spell 'writer'.
    """
    for relpath, func in _PUBLISH_SURFACES.items():
        node = _function_node(relpath, func)
        checks = [
            call for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and (call.func.attr if isinstance(call.func, ast.Attribute)
                 else getattr(call.func, "id", None)) == "check_vault_access"
        ]
        assert checks, f"{func} performs no vault access check"
        for call in checks:
            role = next(
                (kw.value for kw in call.keywords if kw.arg == "required_role"), None,
            )
            assert isinstance(role, ast.Constant) and role.value == "writer", (
                f"{relpath}:{call.lineno} in {func}() no longer requires "
                f"'writer' (got {ast.unparse(role) if role else 'no required_role'}). "
                "The external-git mirror guard in access_service refuses ONLY "
                "required_role == 'writer', so this makes a mirrored document "
                "publishable through the product — and its publication then "
                "survives an upstream delete only by the cascade this file "
                "also tests."
            )


# ── 3. Invariant 2: one INSERT path, inside create_vault's TX ────


def _function_node(relpath: str, name: str):
    tree = ast.parse((_BACKEND / relpath).read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    pytest.fail(f"{name} not found in {relpath}")




def _production_sources() -> list[Path]:
    # `scripts` is in scope deliberately: a one-off "attach a mirror to an
    # existing vault" helper is exactly the shape this guard exists to catch,
    # and that is where one would be written.
    return python_files(("app", "mcp_server", "scripts"))


_INSERT_SIDECAR_RE = re.compile(
    r"INSERT\s+INTO\s+(?:public\.)?\"?vault_external_git\"?", re.IGNORECASE
)


def _sidecar_write_sites() -> dict[tuple[str, str], list[int]]:
    """Every production site that could create a `vault_external_git` row:
    raw INSERT text (any casing/spacing), or a `.create(...)` on a receiver
    that names the sidecar repository.

    Migrations are excluded — they rewrite existing rows under review as a
    schema change, not as a new product path.

    This is a tripwire, not a proof. A scan over source text cannot see
    dynamically composed SQL, a `COPY`, a renamed repository method, or raw
    SQL submitted through `akb_sql` by an unscoped system admin (which
    bypasses every application invariant by design). What it does catch is
    the shape a new FEATURE would take, which is the case this invariant is
    about.
    """
    sites: dict[tuple[str, str], list[int]] = {}
    for path in _production_sources():
        if "migrations/" in path.as_posix():
            continue
        source = path.read_text()
        if "vault_external_git" not in source and "VaultExternalGitRepository" not in source:
            continue
        tree = ast.parse(source)
        rel = path.relative_to(_BACKEND).as_posix()

        def _add(lineno: int) -> None:
            sites.setdefault((rel, enclosing_function(tree, lineno)), []).append(lineno)

        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if _INSERT_SIDECAR_RE.search(node.value):
                    _add(node.lineno)
            elif isinstance(node, ast.JoinedStr):
                # An f-string splices the table name in at runtime; the
                # literal parts are still visible.
                literal = "".join(
                    v.value for v in node.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                )
                if _INSERT_SIDECAR_RE.search(literal):
                    _add(node.lineno)
            elif isinstance(node, ast.Call):
                fn = node.func
                if not isinstance(fn, ast.Attribute) or fn.attr != "create":
                    continue
                # `VaultExternalGitRepository(pool).create(...)`, or any
                # receiver whose spelling names the sidecar (`ext_repo`,
                # `external_git_repo`, `sidecar_repo` …).
                target = ast.unparse(fn.value).lower()
                if "vaultexternalgitrepository" in target or "ext" in target and "repo" in target:
                    _add(node.lineno)
    return sites


# Keyed by (file, function) with the number of sites each may contain, so a
# SECOND insert inside an already-blessed function fails review too.
_SIDECAR_ALLOWLIST: dict[tuple[str, str], tuple[int, str]] = {
    ("app/repositories/vault_external_git_repo.py", "create"): (1, (
        "The repository itself — the INSERT statement's definition, not a "
        "caller of it."
    )),
    ("app/services/document_service.py", "create_vault"): (1, (
        "Inside the same transaction as the `vaults` row. This is the "
        "invariant: a vault is a mirror from birth, so no document — and "
        "therefore no publication — can predate the mirror flag."
    )),
}


def test_vault_external_git_has_exactly_one_creation_path():
    """Invariant 2. A second way to attach a mirror to a vault fails here.

    It would not be a bug on its own — it would be a feature request
    ("mirror an existing vault"). But it turns the external-git delete
    cascade from unreachable into load-bearing: an existing vault can hold
    published documents, and the moment it becomes a mirror, an upstream
    delete-then-re-add re-points those slugs at different content. If
    you are adding that feature, the cascade tests above are what you must
    keep green, and this list is where you record the decision.
    """
    found = _sidecar_write_sites()
    unlisted = sorted(set(found) - set(_SIDECAR_ALLOWLIST))
    assert not unlisted, (
        f"new `vault_external_git` creation path(s): {unlisted}. See this "
        "test's docstring before adding one."
    )
    stale = sorted(set(_SIDECAR_ALLOWLIST) - set(found))
    assert not stale, f"_SIDECAR_ALLOWLIST entries with no matching site: {stale}"

    grew = {
        k: (len(v), _SIDECAR_ALLOWLIST[k][0])
        for k, v in found.items() if len(v) != _SIDECAR_ALLOWLIST[k][0]
    }
    assert not grew, (
        f"allowlisted function's sidecar-write count changed (found, allowed): "
        f"{grew}"
    )


def test_the_sidecar_row_is_created_in_create_vaults_transaction():
    """The two rows must land together, on ONE connection.

    Were the sidecar inserted outside the transaction that creates the `vaults`
    row — or inside it but on a second connection, which is the same thing to
    PostgreSQL — a crash between them would leave a vault that is writable and
    publishable but about to become a mirror, or a mirror whose vault never
    existed.

    Both facts are asserted about the actual `.create(...)` CALL: that the call
    node sits inside a `transaction()` block that also creates the vault, and
    that it is handed the same `conn`. Matching the repository NAME anywhere in
    the block would pass while the call itself had been moved out, leaving only
    its constructor behind.
    """
    create_vault = _function_node("app/services/document_service.py", "create_vault")

    tx_blocks = [
        n for n in ast.walk(create_vault)
        if isinstance(n, ast.AsyncWith)
        and any("transaction()" in ast.unparse(item.context_expr) for item in n.items)
    ]
    assert tx_blocks, "create_vault opens no transaction"

    def _sidecar_calls(node) -> list[ast.Call]:
        out = []
        for n in ast.walk(node):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "create"
                and "VaultExternalGitRepository" in ast.unparse(n.func.value)
            ):
                out.append(n)
        return out

    def _creates_vault_row(node) -> bool:
        return any(
            isinstance(n, ast.Call) and ast.unparse(n.func).endswith("vault_repo.create")
            for n in ast.walk(node)
        )

    calls = _sidecar_calls(create_vault)
    assert len(calls) == 1, f"expected one sidecar create in create_vault, got {len(calls)}"
    sidecar = calls[0]

    hosting = [
        b for b in tx_blocks
        if any(c is sidecar for c in _sidecar_calls(b)) and _creates_vault_row(b)
    ]
    assert hosting, (
        "the `vault_external_git` create call and the `vaults` insert are not in "
        "the same `async with conn.transaction()` block — a crash between them "
        "leaves a half-created mirror"
    )

    conn_kwarg = next((kw for kw in sidecar.keywords if kw.arg == "conn"), None)
    assert conn_kwarg is not None and ast.unparse(conn_kwarg.value) == "conn", (
        "the sidecar create must run on the transaction's own connection "
        "(`conn=conn`); on any other connection it commits independently and "
        "the atomicity this test is about is gone"
    )
