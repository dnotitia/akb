"""Document resolution must be bound to the publication's own vault_id.

Two places used to derive a document entirely from one text column: they
parse `resource_uri`, pull the vault NAME and the path out of it, and look
the document up by that pair. Neither cross-checked the result against
`publications.vault_id` — the column that actually says which vault the row
belongs to. A publication whose URI names a vault other than its own would
therefore reach that other vault's document under a slug issued against the
first vault:

  - `publication_service.resolve_document_publication` — the content path,
    behind `GET /public/{slug}` and `/download`. Serves the document BODY.
  - the oEmbed title lookup in `routes/public.py::oembed` — serves the
    document title through an unfurl that needs no credentials.

`resolve_file_publication` already bound the equivalent lookup
(`WHERE f.id = $1 AND f.vault_id = $2`, with a comment naming cross-vault
IDOR), as did oEmbed's own file branch — sitting a dozen lines below the
document branch that did not. This file pins the binding for both document
call sites.

**The oEmbed branch is the one worth binding first.** It needs no
credentials and runs with `increment_view=False`. A password-protected
publication is already handled: `oembed` short-circuits to a generic
"Protected AKB publication" card and skips every DB title lookup (labelled
F1 in that function). But that check is about the publication's own
password. Where there is none, the unbound lookup returned the title of a
document in whatever vault the URI happened to name — which need not be the
vault that owns the publication, and that vault made no choice about it.

**This is narrower than the change it sits beside.** That one is about a
publication outliving its document and a later document at the same path
inheriting it. This is about the lookups trusting a text field against
nothing. REST creation rejects a URI whose vault disagrees with the route
vault (`create_publication_route`, the `uri_vault != vault` check), so no
known path produces such a row today — which is the argument for binding it
now, while it is cheap and the whole shape can go rather than one instance.

**How the mismatch is manufactured matters.** The publication is created
through the production entry point, so `resource_uri` is genuinely the string
production stores; only then is `vault_id` moved to the other vault by direct
UPDATE. A hand-written `resource_uri` would let a wrong-shaped URI agree with
a wrong-shaped fix and pass while production matched nothing — the failure
mode recorded in `delete_publications_for_document`.

Both vaults hold a document at the SAME path, with different titles. That is
what separates a fix from a near-miss. Binding to `vault_id` while DROPPING
the URI-name cross-check would also stop the cross-vault serve — by silently
returning the publication's OWN vault's document at that path. That is a
different answer to a corrupt row, not a refusal, and an assertion that only
checked "not the far vault's title" would pass on it. Demanding a refusal
catches both shapes, and the failure message names the title that came back,
so which near-miss is in play is readable off the red run.

Talks to a real Postgres via `AKB_TEST_DSN`; skips when unreachable, unless
`REQUIRE_REAL_PG=1` (set by the DB-backed CI job), where an unreachable
database fails instead of skipping so the gate cannot go quietly green.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from app.exceptions import NotFoundError
from app.repositories.document_repo import DocumentRepository
from app.repositories.vault_repo import VaultRepository


_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:5433/akb",  # pragma: allowlist secret
)

_BACKEND = Path(__file__).resolve().parents[1]

# The document lives at the same path in both vaults, so "which vault" is the
# only thing that distinguishes the two rows. Shapes vary because `doc_uri`
# renders a vault-root document differently from one inside a collection, and
# a binding that is right for one shape and wrong for the other would pass a
# single-shape test.
_PATH_SHAPES = [
    pytest.param("handbook.md", id="vault-root"),
    pytest.param("reports/q3.md", id="one-collection-deep"),
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
    # The service under test resolves its connection through the module-global
    # `get_pool()`; wire it to this pool for the duration of the test.
    from app.db import postgres as pg_mod
    prev = pg_mod._pool
    pg_mod._pool = pool
    try:
        await pg_mod._apply_migrations()
        yield pool
    finally:
        pg_mod._pool = prev
        await pool.close()


async def _make_vault(pool, label: str) -> dict:
    vault_repo = VaultRepository(pool)
    name = f"_vaultbind_{label}_{uuid.uuid4().hex[:8]}"
    vid = await vault_repo.create(
        name=name,
        description="ephemeral unit-test vault",
        git_path=f"/tmp/{name}.git",
        owner_id=None,
    )
    return {"id": vid, "name": name}


@pytest_asyncio.fixture
async def vaults(pool):
    """Two vaults: `home` owns the publication row, `far` is named by its URI."""
    home = await _make_vault(pool, "home")
    far = await _make_vault(pool, "far")
    try:
        yield {"home": home, "far": far}
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM vaults WHERE id = ANY($1::uuid[])",
                [home["id"], far["id"]],
            )


@pytest.fixture(autouse=True)
def _public_base_url(monkeypatch):
    """`create_publication` renders a share URL, which refuses to build
    without a configured public base. Irrelevant to what is under test, but
    required to reach it."""
    from app.services import publication_service
    monkeypatch.setattr(
        publication_service.settings, "public_base_url",
        "https://vault-binding.test.local", raising=False,
    )


@pytest.fixture(autouse=True)
def _git_content(monkeypatch):
    """Serve document bodies from memory.

    Resolution reads the body off the Git worktree; these vaults have no
    repository on disk, and the resolver would fall into its
    `content_unavailable` branch and still return a row. That branch would
    mask a positive assertion on content, so return a body keyed by
    (vault, path) instead — which also makes "whose document came back"
    checkable from the content itself, not just the title.

    The body echoes the `commit` argument too, so a test can tell a read
    pinned to the document's own commit from one that floated to the vault
    HEAD. `commit` defaults to None so a caller that stops passing it fails
    an assertion rather than a signature check.
    """
    from app.services import publication_service

    class _Git:
        @staticmethod
        def read_file(vault_name: str, path: str, commit: str | None = None) -> str:
            return f"# body\n\nvault={vault_name} path={path} commit={commit}\n"

    class _DocService:
        git = _Git()

    monkeypatch.setattr(publication_service, "_get_doc_service", lambda: _DocService())


async def _create_doc(pool, vault: dict, path: str, *, title: str) -> uuid.UUID:
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


async def _internal(slug: str) -> dict:
    """The internal publication dict, the shape the resolver is handed."""
    from app.services.publication_service import get_publication_by_slug
    pub = await get_publication_by_slug(slug)
    assert pub is not None, f"publication {slug} vanished"
    return pub


async def _mismatch(pool, vaults, path: str) -> tuple[str, str]:
    """Build a publication whose `vault_id` disagrees with its `resource_uri`.

    A document at `path` exists in BOTH vaults. The publication is created
    against `far` — so its URI names `far` — and then reassigned to `home`.
    Returns (slug, far_title).

    **`document_id` is cleared first, and that is the whole story of which
    rows this guard still protects.** A publication created today carries the
    id of the document it was resolved from, under a composite
    FK (document_id, vault_id) → documents(id, vault_id): moving the row to
    another vault is refused by PostgreSQL, because `far`'s document is not in
    `home`. So a bound row simply cannot reach the mismatched state below.
    What can is a row whose `document_id` is NULL — a publication predating
    migration 053 that the backfill could not bind unambiguously, which is
    exempt from the FK. Clearing the column is how this manufactures one, and
    those legacy rows are exactly the population the read-side vault binding
    still has to refuse. It stops being reachable at all once the last NULL
    is gone.
    """
    far_title = "Far vault original"
    await _create_doc(pool, vaults["far"], path, title=far_title)
    await _create_doc(pool, vaults["home"], path, title="Home vault document")
    slug = await _publish(vaults["far"]["name"], path)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE publications SET document_id = NULL, vault_id = $1 "
            " WHERE slug = $2",
            vaults["home"]["id"], slug,
        )
    return slug, far_title


# ── The mismatch ─────────────────────────────────────────────────


@pytest.mark.parametrize("path", _PATH_SHAPES)
async def test_resolution_refuses_a_publication_whose_uri_names_another_vault(
    pool, vaults, path,
):
    """The row belongs to `home`; its URI names `far`. Resolution must refuse."""
    from app.services.publication_service import resolve_document_publication

    slug, far_title = await _mismatch(pool, vaults, path)
    publication = await _internal(slug)
    # Guard the setup: the state under test has to actually exist, or the
    # refusal below proves nothing.
    assert vaults["far"]["name"] in publication["resource_uri"]
    assert str(publication["vault_id"]) == str(vaults["home"]["id"])

    with pytest.raises(NotFoundError) as excinfo:
        data = await resolve_document_publication(publication)
        # Report the title that came back, not a dict dump — it names WHICH
        # failure this is. `Far vault original` means the cross-vault serve is
        # still open. `Home vault document` means the vault_id binding landed
        # but the URI-name cross-check was dropped, so a corrupt row now
        # resolves silently to its own vault instead of refusing. Demanding a
        # refusal here catches both; only the message tells them apart.
        pytest.fail(
            "resolution must refuse a publication whose vault_id disagrees "
            "with the vault named in its resource_uri, but it served "
            f"{data.get('title')!r} — content: {data.get('content')!r}"
        )

    assert far_title not in str(excinfo.value), (
        "the 404 must not echo the far vault's document title back"
    )


# ── The mismatch, via oEmbed ─────────────────────────────────────
#
# oEmbed resolves a title on its own — it never calls
# `resolve_document_publication` — so the binding on the content path does
# not reach it. It is called directly rather than through the HTTP stack:
# the route is a plain async function, and going through a TestClient would
# drag in auth middleware and app lifespan for no added coverage of the one
# query under test.


_GENERIC_TITLE = "AKB Publication"


@pytest.mark.parametrize("path", _PATH_SHAPES)
async def test_oembed_refuses_a_publication_whose_uri_names_another_vault(
    pool, vaults, path,
):
    """A mismatched row must unfurl as the generic card, not another vault's
    document title."""
    from app.api.routes.public import oembed

    slug, far_title = await _mismatch(pool, vaults, path)
    publication = await _internal(slug)
    # Guard the setup. `oembed` skips the document lookup entirely when the
    # publication carries its own title or a password, so a fixture that
    # produced either would make the assertion below pass without ever
    # reaching the query under test.
    assert not publication.get("title"), "the fixture must force the DB lookup"
    assert not publication.get("password_hash"), "F1 would short-circuit the lookup"
    assert vaults["far"]["name"] in publication["resource_uri"]
    assert str(publication["vault_id"]) == str(vaults["home"]["id"])

    card = await oembed(url=f"https://vault-binding.test.local/p/{slug}")

    assert card["title"] != far_title, (
        "oEmbed disclosed the title of a document in the vault NAMED by the "
        "URI, which is not the vault this publication belongs to"
    )
    assert card["title"] != "Home vault document", (
        "the vault_id binding landed but the URI-name cross-check was dropped: "
        "a corrupt row now unfurls its own vault's document at that path "
        "instead of falling through to the generic card"
    )
    assert card["title"] == _GENERIC_TITLE


async def test_oembed_still_titles_an_ordinary_publication(pool, vaults):
    """The binding must cost a legitimate unfurl nothing — a decoy document at
    the same path in the other vault must not change the answer."""
    from app.api.routes.public import oembed

    path = "reports/q3.md"
    await _create_doc(pool, vaults["far"], path, title="Decoy in the other vault")
    await _create_doc(pool, vaults["home"], path, title="The published document")
    slug = await _publish(vaults["home"]["name"], path)

    card = await oembed(url=f"https://vault-binding.test.local/p/{slug}")

    assert card["title"] == "The published document"


# ── The negative: ordinary publications are untouched ────────────


@pytest.mark.parametrize("path", _PATH_SHAPES)
async def test_an_ordinary_publication_still_resolves(pool, vaults, path):
    """The binding must cost a legitimate resolution nothing.

    Same fixture as the mismatch — a same-path document exists in the other
    vault too — so this also pins that a document elsewhere at that path does
    not disturb the right answer.
    """
    from app.services.publication_service import resolve_document_publication

    await _create_doc(pool, vaults["far"], path, title="Decoy in the other vault")
    await _create_doc(pool, vaults["home"], path, title="The published document")
    slug = await _publish(vaults["home"]["name"], path)

    data = await resolve_document_publication(await _internal(slug))

    assert data["title"] == "The published document"
    assert data["content_unavailable"] is False
    assert f"vault={vaults['home']['name']}" in data["content"], (
        "the body must come from the publication's own vault"
    )
    assert data["resource_type"] == "document"


# ── Identity outlives the URI ────────────────────────────────────
#
# The two lookups above are bound to the publication's `vault_id`, which
# settles WHICH VAULT. It does not settle WHICH DOCUMENT: both still found
# the row by `d.path` parsed out of `resource_uri`, and `documents` is
# UNIQUE(vault_id, path), so a path names whatever occupies it now rather
# than what was published.
#
# `create_publication` refuses to store a publication whose `document_id` and
# `resource_uri` disagree, so they agree when the row is written. Nothing
# keeps them agreeing afterwards except one statement — the move-time URI
# rewrite in `document_service.move`. That is a single place that has to
# remember, which is the shape this branch exists to remove, and the FK
# cannot help because it constrains a column the read path did not read.
#
# So the desync below is written in raw SQL. That is not a shortcut around a
# product path; it IS the threat model — a direct psql session, a future
# migration, an admin script, or a fourth move path that forgets the rewrite.
# The squatter is inserted raw for a second reason: creating it through the
# repository is REFUSED by `assert_no_orphan_publication_for_document`, which
# is the product working. These tests are about what happens when something
# has already got past that.


async def _raw_insert_doc(pool, vault: dict, path: str, *, title: str) -> uuid.UUID:
    """Insert a documents row bypassing the repository's orphan guard."""
    doc_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO documents (id, vault_id, path, title, doc_type, status, "
            "created_at, updated_at, current_commit, tags, metadata) VALUES "
            "($1, $2, $3, $4, 'note', 'draft', NOW(), NOW(), 'c', "
            "'{}'::text[], '{}'::jsonb)",
            doc_id, vault["id"], path, title,
        )
    return doc_id


async def _desync_uri_from_document(pool, vaults, published_path: str, moved_to: str):
    """Publish a document, then move it WITHOUT rewriting the publication's
    URI, and drop a different document onto the path it vacated.

    Returns (slug, published_doc_id, squatter_doc_id).
    """
    published_id = await _create_doc(
        pool, vaults["home"], published_path, title="The published document",
    )
    slug = await _publish(vaults["home"]["name"], published_path)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET path = $1 WHERE id = $2", moved_to, published_id,
        )
    squatter_id = await _raw_insert_doc(
        pool, vaults["home"], published_path, title="Never published by anyone",
    )
    return slug, published_id, squatter_id


@pytest.mark.parametrize("path", _PATH_SHAPES)
async def test_resolution_follows_the_document_not_the_stale_path(pool, vaults, path):
    """A bound publication whose `resource_uri` has gone stale must still serve
    the document it was published for.

    This is what `document_id` is FOR. Pre-change this is RED and serves
    "Never published by anyone" — the body, title, tags and author attribution
    of a document whose owner made no publishing choice at all — because the
    lookup asked which document sits at the published path rather than which
    document was published.
    """
    from app.services.publication_service import resolve_document_publication
    from app.services.uri_service import doc_uri

    moved_to = "archive/moved-away.md"
    slug, published_id, squatter_id = await _desync_uri_from_document(
        pool, vaults, path, moved_to,
    )
    publication = await _internal(slug)

    # Guard the setup: the row must be BOUND and its URI must be STALE, or
    # this test proves nothing about which of the two the lookup followed.
    home = vaults["home"]["name"]
    assert publication["document_id"] == str(published_id), (
        "the publication must carry the published document's id"
    )
    assert publication["resource_uri"] == doc_uri(home, path), (
        "the URI must still render the OLD path — that is the desync under test"
    )
    assert publication["resource_uri"] != doc_uri(home, moved_to)

    data = await resolve_document_publication(publication)

    assert data["title"] == "The published document", (
        f"resolution served {data['title']!r}. The publication is bound to the "
        "document its publisher chose; following the stale path instead serves "
        "a document nobody published."
    )
    assert f"path={moved_to}" in data["content"], (
        "the BODY must be read from the document's current path, not the "
        f"stale one — got {data['content']!r}"
    )


@pytest.mark.parametrize("path", _PATH_SHAPES)
async def test_oembed_follows_the_document_not_the_stale_path(pool, vaults, path):
    """The unfurl must name the same document the body comes from.

    Kept alongside the content test on purpose: a card that titles itself from
    the stale path while the page serves the bound document is two answers to
    one slug.
    """
    from app.api.routes.public import oembed

    from app.services.uri_service import doc_uri

    slug, published_id, _squatter_id = await _desync_uri_from_document(
        pool, vaults, path, "archive/moved-away.md",
    )
    publication = await _internal(slug)
    assert not publication.get("title"), "the fixture must force the DB lookup"
    assert not publication.get("password_hash"), "F1 would short-circuit the lookup"
    # Same setup guards the content test carries: the row must be BOUND and
    # its URI STALE, or a green run here says nothing about which branch
    # answered.
    assert publication["document_id"] == str(published_id)
    assert publication["resource_uri"] == doc_uri(vaults["home"]["name"], path)

    card = await oembed(url=f"https://vault-binding.test.local/p/{slug}")

    assert card["title"] == "The published document", (
        f"oEmbed titled the card {card['title']!r} — the document now sitting "
        "at the published path, not the one that was published"
    )


# ── The legacy branch still works, and is still vault-scoped ─────
#
# `document_id` is nullable: rows predating migration 053 that the backfill
# could not bind unambiguously are exempt from the FK and carry NULL. For
# those the URI is still the only handle, so the path branch has to keep
# working — and keep its vault-name cross-check, which is what the two
# `_mismatch` tests above now exercise (they clear `document_id` precisely so
# the legacy branch is the one under test).


@pytest.mark.parametrize("path", _PATH_SHAPES)
async def test_a_legacy_unbound_publication_still_resolves_by_path(pool, vaults, path):
    """A NULL `document_id` row must still serve its document.

    Without this the change would silently 404 every publication the backfill
    left unbound — the population that has been public the longest.
    """
    from app.services.publication_service import resolve_document_publication

    await _create_doc(pool, vaults["far"], path, title="Decoy in the other vault")
    await _create_doc(pool, vaults["home"], path, title="The published document")
    slug = await _publish(vaults["home"]["name"], path)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE publications SET document_id = NULL WHERE slug = $1", slug,
        )

    publication = await _internal(slug)
    assert publication["document_id"] is None, "the fixture must exercise the NULL branch"

    data = await resolve_document_publication(publication)

    assert data["title"] == "The published document"
    assert f"vault={vaults['home']['name']}" in data["content"], (
        "the legacy branch must still be scoped to the publication's own vault"
    )


async def test_resolution_reads_the_body_at_the_documents_own_commit(pool, vaults):
    """Selecting the right row is only half of serving the right document.

    The query establishes identity by `document_id`, then the connection is
    released and the body is read from Git. Reading the floating vault HEAD at
    `d.path` hands the identity straight back: a path is reusable, so a move
    plus a new document onto the vacated path between the two reads serves
    THAT document's bytes under this publication's title, tags and author.

    `DocumentService.get` pins its read to the row's `current_commit` for the
    same reason, recorded there as E03. The anonymous path is the one that did
    not, which is the read that needs it most.

    Pre-change this is RED with `commit=None` — the tell for a HEAD read.
    """
    from app.services.publication_service import resolve_document_publication

    path = "reports/q3.md"
    await _create_doc(pool, vaults["home"], path, title="The published document")
    slug = await _publish(vaults["home"]["name"], path)

    data = await resolve_document_publication(await _internal(slug))

    # `_create_doc` writes commit_hash="c" * 40, so that is this document's
    # recorded commit — and the fake Git echoes whatever the resolver asked
    # for. `commit=None` means the body came from wherever the vault HEAD
    # happened to be, which is not necessarily this document at all.
    assert f"commit={'c' * 40}" in data["content"], (
        "the body must be read at the document's recorded commit, not the "
        f"floating vault HEAD — got {data['content']!r}"
    )


async def test_a_bound_publication_never_falls_back_to_the_path(pool, vaults):
    """The two branches must be mutually exclusive, not merely ordered.

    A bound row whose `document_id` finds nothing must 404 rather than serve
    whatever occupies its published path. Note this state is UNREACHABLE in
    the table — the composite FK cascades the publication away with its
    document, which is the point of having it — so the publication dict is
    doctored directly. That makes this a test of the predicate rather than of
    a live shape: it fails if the `$2::uuid IS NULL` guard is ever dropped,
    which would make both branches eligible and leave which document gets
    served up to whichever row PostgreSQL happened to return first.
    """
    from app.services.publication_service import resolve_document_publication

    path = "reports/q3.md"
    await _create_doc(pool, vaults["home"], path, title="The path occupant")
    slug = await _publish(vaults["home"]["name"], path)

    publication = dict(await _internal(slug))
    publication["document_id"] = str(uuid.uuid4())  # names no document

    with pytest.raises(NotFoundError):
        data = await resolve_document_publication(publication)
        pytest.fail(
            "a bound publication whose document_id matches nothing must 404; "
            f"it fell back to the path and served {data.get('title')!r}"
        )
