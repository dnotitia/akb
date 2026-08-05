"""Publication service — unified public sharing for documents, tables, and files.

Generates short URL slugs that allow unauthenticated access to a specific
resource. Supports expiration, password protection, view count limits,
snapshot mode, and section filters.

The single `publications` table holds all publication metadata. Resolution
happens via slug lookup; access control is enforced at resolve time.
"""

from __future__ import annotations

import json
import logging
import asyncio
import math
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import asyncpg
import bcrypt
import frontmatter

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import AKBError, ConflictError, NotFoundError
from app.services import file_service, table_service
from app.services.s3_delete_worker import enqueue_delete as _enqueue_s3_delete
from app.services.document_service import DocumentService
from app.services.revision_backend import get_document_service
from app.services.uri_service import parse_uri

logger = logging.getLogger("akb.publications")


# ============================================================
# Constants — single source of truth, no magic strings
# ============================================================

class ResourceType:
    DOCUMENT = "document"
    TABLE_QUERY = "table_query"
    FILE = "file"
    ALL = ("document", "table_query", "file")


class Mode:
    LIVE = "live"
    SNAPSHOT = "snapshot"


def _share_url(slug: str) -> str:
    """Absolute share URL for ``slug``.

    Lifespan refuses to start the app if ``AKB_PUBLIC_BASE_URL`` is unset —
    so by the time any request reaches this helper, ``settings.public_base_url``
    is a non-empty origin and ``share_url`` is guaranteed absolute. No nullable
    return type, no client-side fallback chain.
    """
    base = settings.public_base_url.rstrip("/")
    if not base:
        raise RuntimeError(
            "AKB_PUBLIC_BASE_URL is required at startup but was empty. "
            "Set it to the ingress origin (e.g. https://akb.example.com)."
        )
    return f"{base}/p/{slug}"

# Singleton DocumentService — cheap to instantiate but holds a Git client
# we don't want to recreate on every request.
_doc_service: DocumentService | None = None


def _get_doc_service() -> DocumentService:
    global _doc_service
    if _doc_service is None:
        _doc_service = get_document_service()
    return _doc_service


# ============================================================
# Errors
# ============================================================

class PublicationError(AKBError):
    """Base class for publication-related errors with HTTP status."""
    pass


class PublicationNotFound(PublicationError):
    def __init__(self, slug: str):
        super().__init__(f"Publication not found: {slug}", status_code=404)


class PublicationExpired(PublicationError):
    def __init__(self):
        super().__init__("This publication has expired", status_code=410)


class PublicationViewLimitReached(PublicationError):
    def __init__(self):
        super().__init__("View limit reached for this publication", status_code=410)


class PublicationPasswordRequired(PublicationError):
    def __init__(self):
        super().__init__("Password required", status_code=401)


class PublicationPasswordInvalid(PublicationError):
    def __init__(self):
        super().__init__("Invalid password", status_code=401)


# ============================================================
# Helpers
# ============================================================

def _generate_slug() -> str:
    """Generate URL-safe random slug (12 bytes ≈ 16 chars)."""
    return secrets.token_urlsafe(12)


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


async def _hash_password_async(password: str) -> str:
    # Offload bcrypt off the event loop — see auth_service.hash_password_async.
    return await asyncio.to_thread(_hash_password, password)


async def _verify_password_async(password: str, password_hash: str) -> bool:
    return await asyncio.to_thread(_verify_password, password, password_hash)


def _verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_PARAM_PLACEHOLDER = re.compile(r":([a-zA-Z_][a-zA-Z0-9_]*)")


def parse_expires_in(expires_in: str | None) -> datetime | None:
    """Parse expiration string ('1h','7d','30d','never') → absolute datetime or None."""
    if not expires_in or expires_in.lower() in ("never", "none", ""):
        return None
    m = re.match(r"^(\d+)\s*([smhdw])$", expires_in.strip().lower())
    if not m:
        raise ValueError(f"Invalid expires_in format: {expires_in!r}. Use '1h', '7d', '30d', or 'never'.")
    n = int(m.group(1))
    return datetime.now(timezone.utc) + timedelta(seconds=n * _DURATION_UNITS[m.group(2)])


# Keys passed through from the row into the public response as-is. The
# final public dict is this set plus the two derived keys added by
# ``to_public_dict``: ``share_url`` (absolute URL built from the slug)
# and ``password_protected`` (boolean derived from ``password_hash``).
# Add new public-facing columns here, not in two places.
_PUBLIC_PASSTHROUGH_FIELDS = (
    "slug",
    "resource_type",
    "resource_uri",
    "vault",
    "title",
    "mode",
    "expires_at",
    "max_views",
    "view_count",
    "allow_embed",
    "section_filter",
    "snapshot_at",
    "created_at",
    "query_sql",
    "query_vault_names",
    "query_params",
)


def _row_to_internal_dict(row) -> dict:
    """Normalize a ``publications`` row (joined with ``vaults v`` so the row
    carries ``v.name AS vault``) into a JSON-friendly dict.

    Every read query uses `SELECT p.*, v.name AS vault FROM publications p
    JOIN vaults v ON v.id = p.vault_id` so the row always carries the
    vault name; no separate join in this helper.

    The returned dict is the *internal* shape used by routes / resolvers
    that still need ``password_hash`` (auth check), ``snapshot_s3_key``
    (S3 fetch), ``id`` (advisory lock, internal updates), etc. It is NOT
    safe to return to API/MCP clients — feed it through ``to_public_dict``
    at the response boundary.

    Normalizations:
    - UUID columns → str
    - datetime columns → ISO string
    - ``query_params`` parsed from JSON string into a dict
    """
    d = dict(row)
    for k, v in list(d.items()):
        if isinstance(v, uuid.UUID):
            d[k] = str(v)
        elif hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    # query_params may arrive as a JSON string (asyncpg default for jsonb)
    # or as an already-parsed dict (some asyncpg codec configurations).
    # Normalize both shapes plus the NULL row case to a real dict.
    qp = d.get("query_params")
    if isinstance(qp, str):
        try:
            d["query_params"] = json.loads(qp)
        except (json.JSONDecodeError, TypeError) as e:
            raise PublicationError(
                f"Corrupt query_params JSON for publication {d.get('slug', '?')}: {e}",
                status_code=500,
            )
    elif qp is None:
        d["query_params"] = {}
    elif not isinstance(qp, dict):
        raise PublicationError(
            f"Unexpected query_params type {type(qp).__name__} for publication {d.get('slug', '?')}",
            status_code=500,
        )
    return d


def to_public_dict(internal: dict) -> dict:
    """Internal publication dict → the single canonical public response shape.

    Every external surface (MCP tools, REST API responses, frontend list)
    sees exactly this dict. Internal-only fields (``id``, ``vault_id``,
    ``password_hash``, ``snapshot_s3_key``, ``created_by``, ``updated_at``)
    are stripped here. ``share_url`` is always an absolute URL
    (``AKB_PUBLIC_BASE_URL`` is startup-required).

    Callers must NEVER hand-build a publication response dict — go through
    this function so the shape stays single-source-of-truth.
    """
    out: dict = {k: internal.get(k) for k in _PUBLIC_PASSTHROUGH_FIELDS}
    out["share_url"] = _share_url(internal["slug"])
    out["password_protected"] = bool(internal.get("password_hash"))
    return out


def to_uuid(value) -> uuid.UUID:
    """Coerce a string or UUID into a uuid.UUID. Public helper used by routes."""
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


# ============================================================
# CRUD
# ============================================================

async def create_publication(
    *,
    vault_id: uuid.UUID,
    resource_type: str,
    resource_uri: str | None = None,
    query_sql: str | None = None,
    query_vault_names: list[str] | None = None,
    query_params: dict | None = None,
    password: str | None = None,
    max_views: int | None = None,
    expires_at: datetime | None = None,
    title: str | None = None,
    section_filter: str | None = None,
    allow_embed: bool = True,
    created_by: uuid.UUID | None = None,
) -> dict:
    """Create a publication row. Returns the canonical public dict
    (``slug``, ``share_url``, …) — same shape ``list_publications`` and
    ``akb_publication_snapshot`` return.

    All validation lives here — routes are thin adapters that pass parsed
    arguments. Raises ValueError for any invalid input.

    Every publication is created with ``mode='live'``. Snapshot is a
    table_query-only state transition reached through
    ``create_snapshot(slug=...)`` after the fact — it is not a create-time
    option, which keeps the create surface free of a field that means
    nothing for document/file publications.

    `resource_uri` is the canonical handle for the publishable resource.
    Required for `resource_type ∈ {document, file}`. For `table_query`,
    `resource_uri` stays None — the publishable surface is the SQL itself.
    """
    if resource_type not in ResourceType.ALL:
        raise ValueError(f"Invalid resource_type: {resource_type}")

    # Validate that the right resource fields are present
    if resource_type == ResourceType.DOCUMENT and not resource_uri:
        raise ValueError("resource_uri is required for resource_type='document'")
    if resource_type == ResourceType.FILE and not resource_uri:
        raise ValueError("resource_uri is required for resource_type='file'")
    if resource_type == ResourceType.TABLE_QUERY:
        if not query_sql:
            raise ValueError("query_sql is required for resource_type='table_query'")
        # Only SELECT/WITH allowed
        if not query_sql.strip().upper().startswith(("SELECT", "WITH")):
            raise ValueError(
                "Only SELECT/WITH queries are allowed for publications "
                "(INSERT, UPDATE, DELETE, DDL not permitted)"
            )
        # Reject multi-statement
        if ";" in query_sql.rstrip(";").strip():
            raise ValueError("Multi-statement SQL is not allowed in publications")
        # Every :param in the SQL must be declared in query_params (and vice
        # versa) — catches typos at create time instead of silent runtime drops.
        sql_param_names = set(_PARAM_PLACEHOLDER.findall(query_sql))
        declared_names = set((query_params or {}).keys())
        missing_in_decl = sql_param_names - declared_names
        if missing_in_decl:
            raise ValueError(
                f"SQL references undeclared parameter(s): {sorted(missing_in_decl)}. "
                "Declare them in query_params."
            )
        unused_in_sql = declared_names - sql_param_names
        if unused_in_sql:
            raise ValueError(
                f"query_params declares unused parameter(s): {sorted(unused_in_sql)}. "
                "Remove them or reference them in the SQL."
            )

    slug = _generate_slug()
    pwd_hash = await _hash_password_async(password) if password else None

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Re-check resource existence INSIDE the publish TX, against
            # the canonical resource_uri the caller resolved. Closes the
            # publish/delete race: without this, `document_service.delete`
            # could cascade-clean publications (zero rows) before we
            # INSERT below, leaving an orphan publication that points
            # at a now-gone resource.
            #
            # The check holds the row in a snapshot for the duration of
            # the TX. Concurrent delete blocks on the row lock until
            # after our INSERT commits — meaning either the publication
            # lands first and delete cleans it, or delete commits first
            # and we abort with ResourceVanished.
            if resource_type == ResourceType.DOCUMENT and resource_uri:
                parsed = parse_uri(resource_uri)
                doc_path_for_check = (
                    parsed.identifier if parsed and parsed.kind == "doc" else None
                )
                if doc_path_for_check is not None:
                    found = await conn.fetchval(
                        "SELECT 1 FROM documents WHERE vault_id = $1 AND path = $2 FOR SHARE",
                        vault_id, doc_path_for_check,
                    )
                    if not found:
                        raise ValueError(
                            f"Document not found (resource was deleted concurrently): {resource_uri}"
                        )
            elif resource_type == ResourceType.FILE and resource_uri:
                file_uuid_for_check = resource_uri.rsplit("/", 1)[-1]
                try:
                    file_uuid_obj = uuid.UUID(file_uuid_for_check)
                except ValueError:
                    file_uuid_obj = None
                if file_uuid_obj is not None:
                    # Bind to vault_id (not id alone) so this can't be satisfied
                    # by a same-UUID file in another vault (cross-vault IDOR).
                    found = await conn.fetchval(
                        "SELECT 1 FROM vault_files WHERE id = $1 AND vault_id = $2 FOR SHARE",
                        file_uuid_obj, vault_id,
                    )
                    if not found:
                        raise ValueError(
                            f"File not found (resource was deleted concurrently): {resource_uri}"
                        )

            row = await conn.fetchrow(
                """
                WITH inserted AS (
                    INSERT INTO publications (
                        slug, vault_id, resource_type, resource_uri,
                        query_sql, query_vault_names, query_params,
                        password_hash, max_views, expires_at,
                        mode, section_filter, allow_embed, title, created_by
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                            'live', $11, $12, $13, $14)
                    RETURNING *
                )
                SELECT p.*, v.name AS vault
                  FROM inserted p JOIN vaults v ON v.id = p.vault_id
                """,
                slug, vault_id, resource_type, resource_uri,
                query_sql, query_vault_names, json.dumps(query_params or {}),
                pwd_hash, max_views, expires_at,
                section_filter, allow_embed, title, created_by,
            )

    logger.info("Publication created: %s (type=%s)", slug, resource_type)
    return to_public_dict(_row_to_internal_dict(row))


async def create_publication_for_vault(
    *,
    vault_name: str,
    resource_type: str,
    doc_id: str | None = None,
    file_id: str | None = None,
    query_sql: str | None = None,
    query_vault_names: list[str] | None = None,
    query_params: dict | None = None,
    password: str | None = None,
    max_views: int | None = None,
    expires_in: str | None = None,
    title: str | None = None,
    section_filter: str | None = None,
    allow_embed: bool = True,
    created_by: uuid.UUID | None = None,
) -> dict:
    """High-level helper used by both REST routes and MCP handlers.

    Resolves vault name → vault_id, doc_id → document UUID, validates
    file_id format, parses expires_in, then delegates to create_publication.
    Raises ValueError for any invalid input (caller maps to HTTP/MCP error).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        vault_row = await conn.fetchrow("SELECT id FROM vaults WHERE name = $1", vault_name)
        if not vault_row:
            raise ValueError(f"Vault not found: {vault_name}")
        vault_id = vault_row["id"]

    from app.services.uri_service import doc_uri, file_uri

    resource_uri: str | None = None
    resolved_query_vaults = query_vault_names

    if resource_type == ResourceType.DOCUMENT:
        if not doc_id:
            raise ValueError("doc_id required for resource_type='document'")
        # Resolve the caller's `doc_id` (path, UUID, or d-prefix id —
        # find_by_ref_with_conn handles all three) to the canonical
        # path under this vault, then build the URI.
        from app.repositories.document_repo import DocumentRepository
        doc_repo = DocumentRepository(pool)
        async with pool.acquire() as conn:
            doc_row = await doc_repo.find_by_ref_with_conn(conn, vault_id, doc_id)
        if not doc_row:
            raise ValueError(f"Document not found: {doc_id}")
        resource_uri = doc_uri(vault_name, doc_row["path"])
    elif resource_type == ResourceType.FILE:
        if not file_id:
            raise ValueError("file_id required for resource_type='file'")
        try:
            # Validate UUID format and round-trip back as a string
            # for the URI tail.
            uuid.UUID(file_id)
        except ValueError:
            raise ValueError("Invalid file_id format")
        # Resolve the file's collection so the canonical URI includes
        # its location prefix. Vault-root files come back with NULL
        # collection_id — `file_uri` falls through to the root form.
        async with pool.acquire() as conn:
            file_coll_row = await conn.fetchrow(
                """
                SELECT c.path AS collection
                  FROM vault_files f
                  LEFT JOIN collections c ON c.id = f.collection_id
                 WHERE f.id = $1 AND f.vault_id = $2
                """,
                uuid.UUID(file_id), vault_id,
            )
        # No row means the file is NOT in this vault. REJECT — otherwise a writer
        # could publish a file UUID belonging to another vault and anonymously
        # serve that vault's file through their own publication (cross-vault
        # IDOR). A vault-root file returns a row with collection=NULL (still
        # truthy), so this only rejects genuine cross-vault / missing files.
        if file_coll_row is None:
            raise ValueError(f"File not found in vault: {file_id}")
        file_collection = file_coll_row["collection"]
        resource_uri = file_uri(vault_name, file_id, collection=file_collection)
    elif resource_type == ResourceType.TABLE_QUERY:
        if not resolved_query_vaults:
            resolved_query_vaults = [vault_name]
    else:
        raise ValueError(f"Invalid resource_type: {resource_type}")

    expires_at = parse_expires_in(expires_in)

    return await create_publication(
        vault_id=vault_id,
        resource_type=resource_type,
        resource_uri=resource_uri,
        query_sql=query_sql,
        query_vault_names=resolved_query_vaults,
        query_params=query_params,
        password=password,
        max_views=max_views,
        expires_at=expires_at,
        title=title,
        section_filter=section_filter,
        allow_embed=allow_embed,
        created_by=created_by,
    )


async def delete_publication(
    *,
    slug: str,
    expected_vault_id: uuid.UUID | None = None,
) -> bool:
    """Delete a publication by slug. Returns True if a row was removed.

    `expected_vault_id` binds the delete to a vault: the row is removed
    only if it belongs to that vault. Callers that authorized the request
    against a specific vault MUST pass it, otherwise a writer on vault A
    could delete any publication by guessing its slug (IDOR).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if expected_vault_id is not None:
                row = await conn.fetchrow(
                    "DELETE FROM publications WHERE slug = $1 AND vault_id = $2"
                    " RETURNING snapshot_s3_key",
                    slug, expected_vault_id,
                )
            else:
                row = await conn.fetchrow(
                    "DELETE FROM publications WHERE slug = $1 RETURNING snapshot_s3_key",
                    slug,
                )
            deleted = row is not None
            # Reclaim the cached snapshot object so unpublishing a snapshot-mode
            # table_query doesn't leave it orphaned in S3. Same TX as the delete
            # via the crash-safe outbox, so a crash can't drop one without the
            # other. (publish-hardening F7.)
            if deleted and row["snapshot_s3_key"]:
                await _enqueue_s3_delete(conn, row["snapshot_s3_key"])
    if deleted:
        logger.info("Publication deleted: %s", slug)
    return deleted


@asynccontextmanager
async def _connection(conn):
    """Yield the caller's connection when one is supplied, otherwise a
    freshly acquired pool connection released on exit.

    The cleanup helpers below run several statements, so the usual
    ``if conn is None: … else: …`` shape (see ``DocumentRepository.create``)
    would mean two copies of the body — and one copy silently drifting onto
    the wrong connection is exactly the bug this parameter exists to
    prevent. One body, one connection, no drift.
    """
    if conn is not None:
        yield conn
        return
    pool = await get_pool()
    async with pool.acquire() as own_conn:
        yield own_conn


async def delete_publications_for_document(
    resource_uri: str,
    *,
    expected_vault_id: uuid.UUID | None = None,
    conn=None,
) -> int:
    """Delete all publications for a document, identified by its canonical
    ``akb://`` URI. Returns the number of publications deleted.

    **URI only — deliberately.** This used to accept a PG UUID as well, and
    the two forms carried *different ordering requirements* that nothing in
    the signature revealed: the URI form touches no other table and is safe
    to call at any point, while the UUID form joined ``documents`` to
    materialize the URI. Called on the same connection after the row was
    deleted, that join found nothing and the helper returned 0 — no rows
    removed, no error raised, tests green, publication orphaned. Every
    delete path was therefore correct only by accident of which argument it
    happened to pass, and changing a call from a URI to ``row["id"]`` would
    read as a tidy-up while silently restoring the defect at that path. The
    ordering requirement is gone because the input form that had one is
    gone. Callers that hold only an id resolve it themselves *before*
    deleting — see ``DocumentRepository.delete_with_publications``, which
    reads the path back from the row it has locked.

    ``expected_vault_id`` adds an explicit vault binding to the DELETE,
    matching what ``delete_publication(slug=…, expected_vault_id=…)`` does.
    The URI itself already encodes the vault, so this is belt-and-suspenders
    against any future code path that could fan a URI out across vaults.

    ``conn`` lets a caller that is already deleting the document inside its
    own transaction run this cleanup on that same connection. That is a
    correctness requirement, not a convenience: on a separate connection the
    two deletes commit independently, and a crash in between leaves a
    publication row pointing at a document that no longer exists. Because
    ``documents`` is ``UNIQUE(vault_id, path)`` and publications resolve by
    path, the next document created at that path would then be reached
    through the old public link.
    """
    # Fail loudly rather than deleting nothing. A bare UUID (or any non-doc
    # URI) matches no `resource_uri` and would otherwise return a perfectly
    # calm 0 — the exact silent no-op this signature exists to prevent.
    #
    # This check is for a CALLER-SUPPLIED string. Internal callers that have
    # just built the URI from a locked row go through
    # `delete_publications_by_doc_uri` instead and skip it — see that function
    # for why parsing there would be actively harmful.
    parsed = parse_uri(resource_uri) if isinstance(resource_uri, str) else None
    if parsed is None or parsed.kind != "doc":
        raise ValueError(
            "delete_publications_for_document requires a canonical document "
            f"URI (akb://<vault>/…/doc/<name>), got {resource_uri!r}. "
            "Resolve an id to its URI BEFORE deleting the row — resolving it "
            "after leaves the publication behind."
        )
    return await delete_publications_by_doc_uri(
        resource_uri, expected_vault_id=expected_vault_id, conn=conn,
    )


async def delete_publications_by_doc_uri(
    resource_uri: str,
    *,
    expected_vault_id: uuid.UUID | None = None,
    conn=None,
) -> int:
    """Delete by URI equality, without validating the URI.

    For internal callers that just built ``resource_uri`` with ``doc_uri`` from
    a row they hold a lock on. There is no caller-supplied string to guard
    against, and the DELETE is an equality match that needs no parse.

    **Do not add a parse here.** ``parse_uri`` rejects URIs containing braces
    (template placeholders), and external-git accepts upstream paths like
    ``templates/{{cookiecutter.project}}/README.md`` — so ``doc_uri`` can
    legitimately produce a URI that ``parse_uri`` refuses. Raising on it would
    abort the tombstone, roll the transaction back, and hold the external-git
    sync cursor forever, since a per-file error there is retried and never
    skipped: one templated file upstream would wedge the whole mirror. The
    same reasoning is why ``assert_no_orphan_publication_for_document``
    matches on equality rather than parsing.
    """
    async with _connection(conn) as conn:
        if expected_vault_id is not None:
            rows = await conn.fetch(
                "DELETE FROM publications WHERE resource_uri = $1 AND vault_id = $2 RETURNING id",
                resource_uri, expected_vault_id,
            )
        else:
            rows = await conn.fetch(
                "DELETE FROM publications WHERE resource_uri = $1 RETURNING id",
                resource_uri,
            )
    return len(rows)


async def assert_no_orphan_publication_for_document(
    conn,
    *,
    vault_name: str,
    path: str,
) -> None:
    """Refuse a write that would land a document under a public slug it never
    earned. Returns silently in the healthy case; raises ``ConflictError``
    (409) when ``path``'s canonical URI already carries a publication and no
    document occupies the path.

    **Why a write has to care about publications at all.** A document
    publication is bound to its document only by a path-shaped URI —
    migration 022 dropped the ``document_id`` FK — and ``documents`` is
    ``UNIQUE(vault_id, path)``, so paths are reusable. A publication that
    outlives its document therefore still claims that path: the next
    document to occupy it would be reached through the old link.
    ``DocumentRepository.delete_with_publications`` keeps a delete from
    leaving one behind. This guard covers the other direction — a write
    arriving at a path some earlier state already left a claim on — so the
    two together mean neither end of a path's lifecycle has to trust the
    other to have been correct.

    **The URI is built HERE, from (vault_name, path) — the caller never
    assembles one.** Getting the URI right is the whole check: one that is
    even slightly off-shape matches no publication, the guard silently never
    fires, and the defect survives with green tests. That is not hypothetical —
    the prior incident recorded in ``delete_publications_for_document`` was a
    hand-built legacy URI that never matched what was stored. Its remedy there
    was to accept *only* a URI and validate it; the remedy here is the mirror
    image, and stronger: with the construction inside, there is no wrong URI a
    caller could pass. (The two signatures therefore differ on purpose. Do not
    "harmonize" them: that helper must not look anything up, because its
    callers run it against a row they have already locked, while this one runs
    before a write and has the intended path in hand.)

    **The check is the resolver's own document lookup, inverted.**
    ``resolve_document_publication`` finds the document behind a slug by
    ``d.path = <path from the URI>`` under the vault the URI names, so
    "orphan" here means "the resolver would find nothing" rather than an
    approximation that could drift from it. Keying the anti-join on the vault
    NAME also leaves this helper one vault identifier to be wrong about
    instead of two a caller could pair inconsistently.

    Everything else about the match is a deliberate SUPERSET of what the
    resolver would serve, because a superset fails closed. Publications are
    matched by ``resource_uri`` alone — no ``vault_id`` predicate, no
    ``resource_type`` predicate — so a row stays caught however oddly it is
    bound.

    That makes this match set WIDER than what the cascade removes, in two
    ways, and both are intentional. The cascade deletes one URI (the
    canonical one) bound to one vault; this probes the legacy shape too and
    ignores ``vault_id``. So a row the cascade cannot reach — a legacy-shape
    URI, or one whose ``resource_uri`` and ``vault_id`` disagree — is not
    silently reclaimed here; it blocks the path until someone revokes it.
    A stuck path is the failure we want over a reused one, but it does mean
    the two are not interchangeable: the guard is not a preview of what the
    cascade would delete.

    The resolver has since narrowed — it binds a publication to its own
    ``vault_id`` (see ``resolve_document_publication``) — so this already
    refuses a few writes for rows the resolver would no longer serve. Safe
    direction, and those rows should not exist at all.

    **Which stored URI shapes are matched.** Equality against an index, not a
    parse of every row — so the shapes have to be enumerated. Two are:
    the canonical form, and the pre-0.3.0 nested form
    (``akb://V/doc/reports/q3.md``), which ``parse_uri`` still resolves to the
    same document and which the resolver would therefore still serve.
    Migration 026 rewrote every stored URI to the canonical form, so the
    legacy shape should not exist — but "should not exist" is what left the
    orphans in the incident this file already records, and the probe is one
    index lookup either way. What is NOT matched is a hand-inserted URI with
    trailing slashes (``parse_uri`` strips any number of them, so that family
    is infinite and unindexable); no writer emits one.

    **A path whose canonical URI is unparseable is not special-cased.**
    ``parse_uri`` rejects URIs containing braces (they are template
    placeholders), so a mirrored upstream path like
    ``{{cookiecutter.project}}/README.md`` — which external-git accepts —
    produces a URI nothing can parse. Equality needs no parse, so such a path
    is treated like any other. Deliberately: raising on it instead would hold
    the external-git sync cursor forever (a per-file error there is retried,
    never skipped) over a publication that could not be served anyway, since
    the resolver parses the stored URI too and 404s. The residue is that an
    orphaned brace-path publication still blocks its path — a false conflict
    in the strict sense, fail-closed, and resolved by revoking a dead row.

    **Nothing is deleted.** The publication row carries the slug, its
    creator, its creation time, restrictions and view count — the record an
    owner needs to decide what to do with it. Silently reclaiming the path
    would discard that, so the write loses and a human decides.

    ``conn`` is REQUIRED and must be the connection the write runs on, inside
    the caller's transaction: the check and the write then commit or roll back
    as one unit. That is atomicity, not isolation — under READ COMMITTED each
    statement takes its own snapshot, so a publication inserted between this
    check and the write is not seen.

    What closes that window differs by caller, and not every caller closes it
    fully. ``put`` and ``move`` hold the ``(vault, path)`` advisory lock, but
    on the BASE path — a collision-suffixed path from ``_resolve_free_path``
    is covered by the final ``UNIQUE(vault_id, path)`` rather than the lock.
    ``upsert_external`` holds neither; the external-git sync is single-poller,
    so nothing races it today, but that is a property of the caller and not
    something this helper enforces. The residual race is narrow (a publish
    landing inside one transaction) and fails toward a stale claim rather
    than a reused path.
    """
    from app.services.uri_service import doc_uri

    candidates = [doc_uri(vault_name, path)]
    if "/" in path:
        # Pre-0.3.0 shape: the whole path in the identifier, no `/coll/`
        # segment. `uri_service` deliberately has no builder for it (nothing
        # may emit it), but `parse_uri` still accepts it, so a surviving row
        # in this shape is still servable. Verified against `parse_uri` in
        # tests/test_publication_path_claim_guard_unit.py rather than
        # asserted here.
        candidates.append(f"akb://{vault_name}/doc/{path}")
    # Indexed probe: `idx_publications_resource_uri` (partial, WHERE
    # resource_uri IS NOT NULL — non-null literals are passed, so it applies).
    # The anti-join uses vaults' UNIQUE name and UNIQUE(vault_id, path), and
    # is only reached for rows the outer probe returns — in a healthy database,
    # none. That is the cost this pays on every document create.
    row = await conn.fetchrow(
        """
        SELECT p.slug, p.created_at
          FROM publications p
         WHERE p.resource_uri = ANY($1::text[])
           AND NOT EXISTS (
                   SELECT 1
                     FROM documents d
                     JOIN vaults v ON v.id = d.vault_id
                    WHERE v.name = $2 AND d.path = $3
               )
         ORDER BY p.created_at
         LIMIT 1
        """,
        candidates, vault_name, path,
    )
    if row is None:
        return
    raise ConflictError(
        f"Refusing to write {path!r} in vault {vault_name!r}: the path is "
        f"claimed by a public link whose document no longer exists "
        f"(publication slug {row['slug']}, created "
        f"{row['created_at'].isoformat()}). A document written here would be "
        f"reachable through that link. The publication is left in place "
        f"rather than removed — review it, then "
        f"revoke it (akb_unpublish slug={row['slug']}, or "
        f"DELETE /api/v1/publications/{vault_name}/{row['slug']}) before "
        f"reusing this path."
    )


async def delete_publications_for_file(
    file_id: uuid.UUID | str,
    vault_name: str,
    *,
    expected_vault_id: uuid.UUID | None = None,
    conn=None,
) -> int:
    """Delete all publications for a given file. Looks up the file's
    collection so the URI matches the canonical form stored in the
    ``publications.resource_uri`` column.

    **CALL THIS BEFORE DELETING THE ``vault_files`` ROW.** The lookup below
    reads ``vault_files``; run on the caller's connection after the row is
    deleted, the row is already invisible inside that transaction, so the
    helper builds a *vault-root* URI, matches nothing, and returns 0 — no
    rows removed, no error, tests green. Note this is only wrong for files
    that live in a collection: a vault-root file's root-form URI is correct
    either way, so a call site with the ordering wrong still looks right
    until someone puts a file in a folder. Every deleting caller today
    passes ``conn`` and runs before its row delete; keep it that way.

    ``expected_vault_id`` adds an explicit vault binding to the DELETE
    (see ``delete_publications_for_document`` for rationale).

    ``conn`` runs the lookup and the DELETE inside the caller's transaction
    (same rationale as ``delete_publications_for_document``): on a separate
    connection the two commit independently, and a crash between them leaves
    the orphan the deleting caller is trying to avoid.
    """
    from app.services.uri_service import file_uri
    async with _connection(conn) as conn:
        coll_row = await conn.fetchrow(
            """
            SELECT c.path AS collection
              FROM vault_files f
              JOIN vaults v ON v.id = f.vault_id
              LEFT JOIN collections c ON c.id = f.collection_id
             WHERE f.id = $1 AND v.name = $2
            """,
            uuid.UUID(str(file_id)), vault_name,
        )
        collection = coll_row["collection"] if coll_row else None
        uri = file_uri(vault_name, str(file_id), collection=collection)
        if expected_vault_id is not None:
            rows = await conn.fetch(
                "DELETE FROM publications WHERE resource_uri = $1 AND vault_id = $2 RETURNING id",
                uri, expected_vault_id,
            )
        else:
            rows = await conn.fetch(
                "DELETE FROM publications WHERE resource_uri = $1 RETURNING id",
                uri,
            )
    return len(rows)


# Single FROM clause for every read query. Joining vaults inline means the
# row always carries `vault` (the human-readable name), so the helpers
# never need a second lookup and the public dict has the field clients
# actually want to display.
_PUBLICATION_SELECT = (
    "SELECT p.*, v.name AS vault "
    "FROM publications p JOIN vaults v ON v.id = p.vault_id"
)


async def list_publications(vault_id: uuid.UUID, resource_type: str | None = None) -> list[dict]:
    """List active publications for a vault. Returns public dicts.

    Returns every row, unpaginated. That is a contract two callers rely on —
    see `list_publications_route` before adding a page bound.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if resource_type:
            rows = await conn.fetch(
                f"{_PUBLICATION_SELECT} "
                "WHERE p.vault_id = $1 AND p.resource_type = $2 "
                "ORDER BY p.created_at DESC",
                vault_id, resource_type,
            )
        else:
            rows = await conn.fetch(
                f"{_PUBLICATION_SELECT} "
                "WHERE p.vault_id = $1 "
                "ORDER BY p.created_at DESC",
                vault_id,
            )
    return [to_public_dict(_row_to_internal_dict(r)) for r in rows]


async def get_publication_by_slug(slug: str) -> dict | None:
    """Read publication by slug without enforcement (for inspection).

    Returns the **internal** dict — callers that surface to API/MCP must
    feed through ``to_public_dict``. Returns None if slug not found.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"{_PUBLICATION_SELECT} WHERE p.slug = $1",
            slug,
        )
    if row is None:
        return None
    return _row_to_internal_dict(row)


# ============================================================
# Resolution + Access checks
# ============================================================

async def resolve_publication(
    slug: str,
    password: str | None = None,
    increment_view: bool = True,
    bypass_password: bool = False,
    enforce_view_cap: bool = True,
) -> dict:
    """Look up a publication and enforce access controls.

    Raises:
        PublicationNotFound — slug doesn't exist
        PublicationExpired — past expires_at
        PublicationViewLimitReached — view_count >= max_views
        PublicationPasswordRequired — password set but not provided
        PublicationPasswordInvalid — wrong password

    Returns the **internal** publication dict (includes ``id``,
    ``password_hash``, ``snapshot_s3_key`` — needed by downstream
    resolvers). Surface-facing callers convert via ``to_public_dict``.

    bypass_password: skip the password check (used when caller has already
    verified an HMAC session token at the route layer).

    enforce_view_cap: only meaningful when ``increment_view`` is False. The file
    content endpoints (/raw, /download) RE-serve bytes for a view that was
    already counted at the metadata GET, so they resolve with
    increment_view=False AND enforce_view_cap=False — a page opened at its last
    allowed view must still be able to fetch its bytes (Range requests, reloads,
    preview→download) without a spurious 410. Expiry is always enforced.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"{_PUBLICATION_SELECT} WHERE p.slug = $1",
            slug,
        )
        if row is None:
            raise PublicationNotFound(slug)

        if row["expires_at"] is not None and row["expires_at"] <= datetime.now(timezone.utc):
            raise PublicationExpired()

        if row["password_hash"] and not bypass_password:
            if not password:
                raise PublicationPasswordRequired()
            if not await _verify_password_async(password, row["password_hash"]):
                raise PublicationPasswordInvalid()

        if increment_view:
            # Atomic check + increment in one statement. Pre-fix, the
            # row read at line 571 and the UPDATE here ran as two
            # separate statements with no row lock, so N concurrent
            # readers all saw view_count < max_views and all incremented,
            # overshooting max_views by up to N-1 (06-F8 / 04-F7).
            #
            # Re-check expires_at inside the UPDATE too — between the
            # SELECT above and this UPDATE another caller could
            # post-date `expires_at` via an admin edit, and we don't
            # want to record a view against a publication that became
            # expired in the gap.
            updated = await conn.fetchrow(
                """
                UPDATE publications
                   SET view_count = view_count + 1
                 WHERE id = $1
                   AND (max_views IS NULL OR view_count < max_views)
                   AND (expires_at IS NULL OR expires_at > NOW())
                 RETURNING view_count, max_views, expires_at
                """,
                row["id"],
            )
            if updated is None:
                # Either max_views was reached or expires_at lapsed
                # between the SELECT and the UPDATE. Re-resolve which
                # one to surface so the caller sees the same error class
                # they would have seen with stale data.
                cur = await conn.fetchrow(
                    "SELECT expires_at, view_count, max_views FROM publications WHERE id = $1",
                    row["id"],
                )
                if cur is not None and cur["expires_at"] is not None and \
                        cur["expires_at"] <= datetime.now(timezone.utc):
                    raise PublicationExpired()
                raise PublicationViewLimitReached()
            # Reflect the post-increment counter back so the response is
            # consistent with the value that just landed in PG.
            row = dict(row)
            row["view_count"] = updated["view_count"]
        elif enforce_view_cap:
            if row["max_views"] is not None and row["view_count"] >= row["max_views"]:
                raise PublicationViewLimitReached()

    return _row_to_internal_dict(row)


# ============================================================
# Document resolution
# ============================================================

async def resolve_document_publication(publication: dict) -> dict:
    """Read document content for a document-type publication.

    Returns a dict with the document body and display metadata. Never
    includes the document UUID — the public viewer only knows the slug.
    If the underlying Git file is missing, sets content_unavailable=true
    and returns a placeholder body so consumers can display a clear notice.
    """
    if publication["resource_type"] != ResourceType.DOCUMENT:
        raise PublicationError("Not a document publication", status_code=400)

    # Parse the canonical URI to find the underlying doc row.
    from app.services.uri_service import parse_uri
    uri = publication.get("resource_uri")
    parsed = parse_uri(uri) if uri else None
    if parsed is None or parsed.kind != "doc":
        raise NotFoundError("Document", str(uri))
    uri_vault, doc_path = parsed.vault, parsed.identifier

    pool = await get_pool()
    async with pool.acquire() as conn:
        doc_row = await conn.fetchrow(
            """
            SELECT d.path, d.title, d.doc_type, d.status, d.summary, d.domain,
                   d.created_by, d.created_at, d.updated_at, d.tags,
                   v.name AS vault_name,
                   COALESCE(u.display_name, u.username) AS created_by_name
            FROM documents d
            JOIN vaults v ON d.vault_id = v.id
            -- created_by holds the actor's username on the normal write path
            -- (older rows store a UUID); match either form, mirroring
            -- user_directory.resolve_display_names.
            LEFT JOIN users u
                ON u.id::text = d.created_by OR u.username = d.created_by
            -- Bind to the publication's own vault_id (not the vault NAME
            -- parsed out of resource_uri) so this can't be satisfied by a
            -- same-path document in another vault (cross-vault IDOR) — the
            -- same binding `resolve_file_publication` puts on vault_files.
            -- `v.name = $3` is then the cross-check, not the lookup key: v is
            -- joined on the pinned vault_id, so requiring it to equal the
            -- URI's vault name means a row whose resource_uri disagrees with
            -- its vault_id matches nothing and 404s. Fail closed — serving
            -- the publication's own document at that path instead would be a
            -- silent partial success on a row that is, either way, corrupt.
            WHERE d.vault_id = $1 AND d.path = $2 AND v.name = $3
            """,
            to_uuid(publication["vault_id"]), doc_path, uri_vault,
        )
        if doc_row is None:
            raise NotFoundError("Document", str(uri))

    body = ""
    content_unavailable = False
    try:
        # Git read is blocking filesystem I/O — offload it so a document page
        # open / download never stalls the single event loop (503 risk class).
        raw = await asyncio.to_thread(
            _get_doc_service().git.read_file, doc_row["vault_name"], doc_row["path"]
        )
        if raw:
            body = frontmatter.loads(raw).content
    except (FileNotFoundError, OSError) as e:
        logger.warning("Document content unavailable for publication: %s", e)
        content_unavailable = True
        body = "*Document content is no longer available.*"

    section_filter = publication.get("section_filter")
    section_not_found = False
    if section_filter and body:
        filtered, found = _filter_section(body, section_filter)
        if found:
            body = filtered
        else:
            section_not_found = True
            logger.info("section_filter %r did not match any heading", section_filter)

    return {
        "resource_type": ResourceType.DOCUMENT,
        "title": publication.get("title") or doc_row["title"],
        "type": doc_row["doc_type"],
        "summary": doc_row["summary"],
        "domain": doc_row["domain"],
        # Author attribution via the RESOLVED name only (created_by_name =
        # display_name, or username when no display name) — never the raw
        # created_by, which is a username or a bare user UUID on legacy rows and
        # would hand an anonymous viewer a valid account identifier. Also drop
        # the internal workflow `status` (leaks draft/review state) and the
        # unused `created_at` to minimize the exposed surface. Showing a username
        # when no display name is set remains a product-policy choice for the
        # attribution feature. (publish-hardening F8.)
        "created_by_name": doc_row["created_by_name"],
        "updated_at": doc_row["updated_at"].isoformat() if doc_row["updated_at"] else None,
        "tags": list(doc_row["tags"]) if doc_row["tags"] else [],
        "content": body,
        "content_unavailable": content_unavailable,
        "section_filter": section_filter,
        "section_not_found": section_not_found,
    }


_HEADING_RE = re.compile(r"^(#+)\s+(.*)$")


def _filter_section(markdown: str, section_path: str) -> tuple[str, bool]:
    """Filter markdown to only include the matching heading and its children.

    Matching is case-insensitive and tries exact match first, then substring.
    Walks forward until the next heading at the same or higher level.

    Returns:
        (filtered_markdown, found): `found=True` if a heading matched and
        the filtered subtree is returned. `found=False` if no heading matched
        — in that case the original markdown is returned unchanged so callers
        can choose to surface a "section not found" notice.
    """
    lines = markdown.splitlines()
    target = section_path.strip().lstrip("#").strip().lower()
    if not target:
        return markdown, False

    headings: list[tuple[int, int, str]] = []
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m:
            headings.append((i, len(m.group(1)), m.group(2).strip().lower()))

    # Prefer exact match over substring
    match_idx: int | None = None
    match_level: int | None = None
    for i, level, text in headings:
        if text == target:
            match_idx, match_level = i, level
            break
    if match_idx is None:
        for i, level, text in headings:
            if target in text:
                match_idx, match_level = i, level
                break

    if match_idx is None or match_level is None:
        return markdown, False

    end_idx = len(lines)
    for i, level, _ in headings:
        if i > match_idx and level <= match_level:
            end_idx = i
            break

    return "\n".join(lines[match_idx:end_idx]), True


# ============================================================
# File resolution (P3)
# ============================================================



async def resolve_file_publication(publication: dict) -> dict:
    """Get file metadata + presigned URL (for inline preview of images/PDFs
    rendered via <img>/<embed>). Force-download flows go through
    `get_file_storage_for_publication` and stream via the backend instead.
    """
    if publication["resource_type"] != ResourceType.FILE:
        raise PublicationError("Not a file publication", status_code=400)

    from app.services.uri_service import parse_uri
    uri = publication.get("resource_uri")
    parsed = parse_uri(uri) if uri else None
    if parsed is None or parsed.kind != "file":
        raise NotFoundError("File", str(uri))
    file_uuid_str = parsed.identifier

    pool = await get_pool()
    async with pool.acquire() as conn:
        file_row = await conn.fetchrow(
            """
            SELECT f.name, f.s3_key, f.mime_type, f.size_bytes,
                   c.path AS collection
              FROM vault_files f
              LEFT JOIN collections c ON c.id = f.collection_id
             WHERE f.id = $1 AND f.vault_id = $2
            """,
            to_uuid(file_uuid_str), to_uuid(publication["vault_id"]),
        )
        if file_row is None:
            raise NotFoundError("File", str(uri))

    # No presigned S3 URL is issued: the browser fetches bytes from same-origin
    # /public/{slug}/raw (inline preview) and /public/{slug}/download (force
    # download). A presigned URL would embed the vault name in its S3 key path
    # and stay valid ~5 min after unpublish/view-limit; the proxy paths leak
    # nothing, stay view-counted, and revoke instantly. (publish-hardening F4.)
    return {
        "resource_type": ResourceType.FILE,
        "name": file_row["name"],
        "title": publication.get("title") or file_row["name"],
        "mime_type": file_row["mime_type"],
        "size_bytes": file_row["size_bytes"],
        "collection": file_row["collection"],
    }


async def get_file_storage_for_publication(publication: dict) -> dict:
    """Return s3_key + minimal metadata needed to stream a file publication
    through the backend. Internal use only — never serialize the result to
    clients (s3_key would leak the storage layout).
    """
    if publication["resource_type"] != ResourceType.FILE:
        raise PublicationError("Not a file publication", status_code=400)

    from app.services.uri_service import parse_uri
    uri = publication.get("resource_uri")
    parsed = parse_uri(uri) if uri else None
    if parsed is None or parsed.kind != "file":
        raise NotFoundError("File", str(uri))
    file_uuid_str = parsed.identifier

    pool = await get_pool()
    async with pool.acquire() as conn:
        file_row = await conn.fetchrow(
            "SELECT name, s3_key, mime_type, size_bytes FROM vault_files WHERE id = $1 AND vault_id = $2",
            to_uuid(file_uuid_str), to_uuid(publication["vault_id"]),
        )
    if file_row is None:
        raise NotFoundError("File", str(uri))

    return {
        "name": file_row["name"],
        "s3_key": file_row["s3_key"],
        "mime_type": file_row["mime_type"],
        "size_bytes": file_row["size_bytes"],
    }


# ============================================================
# Table query resolution (P2)
# ============================================================

def _bind_params_to_sql(sql: str, param_defs: dict, url_params: dict) -> tuple[str, list]:
    """Convert :param_name placeholders to $N positional params.

    Returns (rewritten_sql, ordered_param_values).
    Raises ValueError if a required param is missing or type is invalid.
    """
    used_order: list[str] = []

    def replace(match: re.Match) -> str:
        name = match.group(1)
        if name not in param_defs:
            raise ValueError(f"Unknown parameter: {name}")
        used_order.append(name)
        return f"${len(used_order)}"

    # Replace :param_name (only word characters)
    rewritten = _PARAM_PLACEHOLDER.sub(replace, sql)

    # Build ordered values
    values: list = []
    for name in used_order:
        spec = param_defs[name]
        ptype = spec.get("type", "text")
        provided = url_params.get(name)
        if provided is None or provided == "":
            if "default" in spec:
                provided = spec["default"]
            elif spec.get("required"):
                raise ValueError(f"Missing required parameter: {name}")
            else:
                provided = None

        # Type coercion
        if provided is not None:
            try:
                if ptype == "number" or ptype == "int" or ptype == "integer":
                    provided = int(provided) if isinstance(provided, str) else provided
                elif ptype == "float":
                    provided = float(provided) if isinstance(provided, str) else provided
                elif ptype == "boolean" or ptype == "bool":
                    provided = str(provided).lower() in ("true", "1", "yes", "on") if isinstance(provided, str) else bool(provided)
                # else text: leave as-is
            except (ValueError, TypeError) as e:
                raise ValueError(f"Invalid value for parameter {name} (expected {ptype}): {e}")

        values.append(provided)

    return rewritten, values


def _serialize_value(v: Any) -> Any:
    """Coerce a DB (or cached-snapshot) value into a JSON-safe form, recursing
    into arrays/objects. Non-finite floats and Decimals (NaN / ±Inf) become null:
    they are not valid JSON and Starlette's JSONResponse renders with
    allow_nan=False (would 500), so a canned query that produces them — or a
    legacy snapshot that stored them — would break the publication permanently.
    A non-finite value is thereby indistinguishable from SQL NULL in the output.
    (publish-hardening F3; matches akb_sql's inf_nan_mode="null".)
    """
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, Decimal):
        return str(v) if v.is_finite() else None
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (list, tuple)):
        return [_serialize_value(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _serialize_value(x) for k, x in v.items()}
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


# Bounds for an ANONYMOUS public table_query (GET /public/{slug}). Both are DoS
# guards: the timeout caps how long one query can hold a DB connection / stall
# the single event loop; the row cap stops a huge fast result (e.g.
# generate_series) from exhausting process memory. A published query can be
# replayed by anyone with the slug, so these run for every public execution.
_PUBLIC_TABLE_TIMEOUT_MS = 5000
_PUBLIC_TABLE_ROW_CAP = 10_000


async def resolve_table_query_publication(publication: dict, url_params: dict | None = None) -> dict:
    """Execute a canned table query publication with URL parameter binding."""
    if publication["resource_type"] != ResourceType.TABLE_QUERY:
        raise PublicationError("Not a table query publication", status_code=400)

    # Snapshot mode: return cached snapshot from S3 (P4)
    if publication.get("mode") == Mode.SNAPSHOT and publication.get("snapshot_s3_key"):
        return await _read_snapshot(publication)

    sql = publication["query_sql"]
    param_defs = publication.get("query_params") or {}
    vault_names = list(publication.get("query_vault_names") or [publication["vault"]])
    url_params = url_params or {}

    try:
        rewritten_sql, values = _bind_params_to_sql(sql, param_defs, url_params)
    except ValueError as e:
        raise PublicationError(f"Parameter error: {e}", status_code=400)

    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            table_map = await table_service.build_table_name_map(conn, vault_names)
        except NotFoundError as e:
            raise PublicationError(str(e), status_code=404)
        rewritten = table_service.rewrite_table_names(rewritten_sql, table_map)

        if ";" in rewritten.rstrip(";").strip():
            raise PublicationError("Multi-statement SQL is not allowed", status_code=400)

        if not rewritten.strip().upper().startswith(("SELECT", "WITH")):
            raise PublicationError("Only SELECT queries are allowed for publications", status_code=400)

        # Execute under the publication CREATOR's PG role, never the
        # privileged pool role. Without this, a public (unauthenticated)
        # visitor's query runs as the service role and can read system
        # tables (users/tokens) and any vault's vt_* tables — full
        # cross-vault / system-table exfiltration. Running as
        # akb_user_<created_by> makes PG return 42501 for anything the
        # creator could not have read via akb_sql, so a publication can
        # only ever expose what its author was authorized to see.
        from app.services.role_sync import user_role_name
        created_by = publication.get("created_by")
        if not created_by:
            # Legacy publications without a recorded creator cannot be
            # safely scoped — fail closed rather than fall back to the
            # privileged role.
            raise PublicationError(
                "This shared query can no longer be served (no owner on record).",
                status_code=403,
            )
        role = user_role_name(created_by)
        truncated = False
        rows: list[asyncpg.Record] = []
        try:
            async with conn.transaction():
                await conn.execute("SET TRANSACTION READ ONLY")
                # DoS bounds for this anonymous path: cap DURATION so a slow query
                # can't stall the single event loop / pin a DB connection, and cap
                # ROWS (stream via a cursor) so a huge fast result can't OOM the
                # process. Both apply under the creator's PG role.
                await conn.execute(f"SET LOCAL statement_timeout = '{_PUBLIC_TABLE_TIMEOUT_MS}ms'")
                await conn.execute(f'SET LOCAL ROLE "{role}"')
                async for r in conn.cursor(rewritten, *values):
                    if len(rows) >= _PUBLIC_TABLE_ROW_CAP:
                        truncated = True
                        break
                    rows.append(r)
        except asyncpg.exceptions.QueryCanceledError:
            # statement_timeout fired (57014) — don't echo the query.
            raise PublicationError("This shared query took too long to run.", status_code=400)
        except asyncpg.exceptions.InsufficientPrivilegeError:
            # 42501 — the creator's role lacks SELECT on a referenced
            # table (system table or another vault). Do not echo the
            # table name to the public visitor.
            raise PublicationError(
                "This shared query references data that is no longer accessible.",
                status_code=403,
            )
        except asyncpg.exceptions.UndefinedObjectError:
            # SET LOCAL ROLE to a role that no longer exists (creator deleted).
            raise PublicationError(
                "This shared query can no longer be served (owner removed).",
                status_code=403,
            )
        except Exception as e:
            msg = str(e)
            if "read-only transaction" in msg:
                raise PublicationError("Write operations are not allowed", status_code=403)
            # Don't echo the raw DB error to an anonymous public caller — it can
            # name tables/columns/other vaults. Log it for the operator, return
            # a generic message. (The publish-time run already surfaced real
            # query bugs to the authenticated author.)
            logger.warning("public table_query error for slug=%s: %s", publication.get("slug"), msg)
            raise PublicationError("This shared query could not be run.", status_code=400)

    columns = list(dict(rows[0]).keys()) if rows else []
    # Per-cell coercion is CPU-bound and runs for an anonymous caller — offload it
    # so a large (capped) result set can't stall the single event loop.
    def _coerce_rows() -> list[dict]:
        return [{k: _serialize_value(v) for k, v in dict(r).items()} for r in rows]

    result_rows = await asyncio.to_thread(_coerce_rows)

    return {
        "resource_type": ResourceType.TABLE_QUERY,
        "title": publication.get("title") or "Shared query",
        "columns": columns,
        "rows": result_rows,
        "total": len(result_rows),
        # True when the result hit the public row cap and was truncated for the
        # preview — callers can surface "showing first N rows".
        "truncated": truncated,
        "query_params": param_defs,
        "applied_params": {n: url_params.get(n) for n in param_defs},
        "mode": publication["mode"],
    }


# ============================================================
# Snapshot (P4)
# ============================================================

_SNAPSHOT_MAX_BYTES = 128 * 1024 * 1024  # read cap — guard memory on a stray huge snapshot


def _decode_snapshot(raw: bytes) -> dict:
    """Decode + sanitize a snapshot body. Pure CPU — runs off the event loop."""
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Snapshot data is not a valid object")
    # Legacy snapshots written before F3 can hold literal NaN/Infinity (json.loads
    # accepts them as float('nan')/inf), which would 500 on render — sanitize on
    # read so old snapshots resolve instead of being permanently broken. (F3.)
    return _serialize_value(data)


async def _read_snapshot(publication: dict) -> dict:
    """Read a snapshotted query result from S3."""
    try:
        # S3 GET (blocking I/O) AND the JSON decode + F3 sanitize (CPU) both run
        # off the event loop, and the read is capped, so serving an anonymous
        # snapshot can't stall the single loop or OOM on an oversized body.
        body = await asyncio.to_thread(
            file_service.get_object_bytes, publication["snapshot_s3_key"], _SNAPSHOT_MAX_BYTES
        )
        data = await asyncio.to_thread(_decode_snapshot, body)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
        raise PublicationError(f"Corrupt snapshot data: {e}", status_code=500)
    # file_service.StorageError already inherits from AKBError → propagates as 502.

    data["snapshot_at"] = publication.get("snapshot_at")
    data["mode"] = "snapshot"
    return data


async def create_snapshot(
    publication_id: uuid.UUID,
    *,
    expected_vault_id: uuid.UUID | None = None,
) -> dict:
    """Execute a table_query publication's SQL once and store result in S3.

    The publication's `mode` is then flipped to 'snapshot' so subsequent visits
    return the cached result instead of re-running the query.

    `expected_vault_id` binds the snapshot to a vault: callers that
    authorized the request against a specific vault MUST pass it, else a
    writer on vault A could force-execute and snapshot any publication by
    id regardless of owning vault (cross-vault execution).
    """
    pool = await get_pool()
    # Session-scoped advisory lock keyed on the publication id so two
    # concurrent /snapshot calls on the same publication don't both run
    # the (potentially slow) table query, upload to S3 twice, and race
    # on the final UPDATE. Released on connection close.
    lock_key = int.from_bytes(publication_id.bytes[:8], "big", signed=True)
    async with pool.acquire() as lock_conn:
        async with lock_conn.transaction():
            await lock_conn.execute("SELECT pg_advisory_xact_lock($1)", lock_key)

            row = await lock_conn.fetchrow(
                f"{_PUBLICATION_SELECT} WHERE p.id = $1",
                publication_id,
            )
            if row is None:
                raise PublicationNotFound(str(publication_id))
            # Reject cross-vault snapshots BEFORE running the query / S3 write.
            if expected_vault_id is not None and row["vault_id"] != expected_vault_id:
                raise PublicationNotFound(str(publication_id))

            publication = _row_to_internal_dict(row)
            if publication["resource_type"] != ResourceType.TABLE_QUERY:
                raise PublicationError(
                    "Snapshots only supported for table_query publications",
                    status_code=400,
                )

            # Force live execution for the snapshot (regardless of current mode)
            publication_for_exec = {**publication, "mode": Mode.LIVE}
            result = await resolve_table_query_publication(publication_for_exec, {})

            # VERSIONED key (uuid per attempt) so the PUT can be OFFLOADED safely.
            # A sync PUT under the advisory lock blocked the single event loop for
            # the write's duration (~30s on an S3 stall → 503). Offloading it is
            # race-free ONLY with a unique key: if the request is cancelled during
            # the PUT, to_thread lets the zombie finish, but it writes ITS OWN
            # unreferenced key (the rolled-back txn never points at it) instead of
            # overwriting a newer snapshot. The DB CAS below publishes the winner's
            # key; the previous key is enqueued for delete in the SAME transaction.
            old_key = publication.get("snapshot_s3_key")
            s3_key = f"snapshots/{publication_id}/{uuid.uuid4().hex}.json"
            try:
                # allow_nan=False: result is already coerced by _serialize_value,
                # so this just refuses to silently persist a stray non-finite
                # float as invalid JSON. (F3.) Both the CPU serialize and the S3
                # PUT run off the event loop; result is row-capped upstream.
                payload = await asyncio.to_thread(
                    lambda: json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
                )
                await asyncio.to_thread(
                    file_service.put_object_bytes, s3_key, payload, "application/json",
                )
            except (file_service.StorageError, IOError) as e:
                raise PublicationError(f"Failed to upload snapshot: {e}", status_code=502)

            updated_row = await lock_conn.fetchrow(
                """
                WITH bumped AS (
                    UPDATE publications
                       SET snapshot_s3_key = $1, snapshot_at = NOW(),
                           mode = 'snapshot', updated_at = NOW()
                     WHERE id = $2
                    RETURNING *
                )
                SELECT p.*, v.name AS vault
                  FROM bumped p JOIN vaults v ON v.id = p.vault_id
                """,
                s3_key, publication_id,
            )
            if updated_row is None:
                # The publication was unpublished concurrently (delete_publication
                # does NOT take this advisory lock), so the UPDATE matched no row.
                # Our just-uploaded object is now unreferenced — reclaim IT (the
                # old key, if any, was already reclaimed by the delete path). The
                # 404 is raised AFTER this txn commits, so the reclaim isn't rolled
                # back. Previously this crashed on a None row and orphaned the
                # upload (Codex).
                await _enqueue_s3_delete(lock_conn, s3_key)
            elif old_key and old_key != s3_key:
                # Normal path: reclaim the PREVIOUS snapshot object via the durable
                # outbox, committed atomically with the new key (rolls back if the
                # snapshot txn does). A crash before the reaper runs orphans one
                # object (storage cost only; the DB points at the new key).
                await _enqueue_s3_delete(lock_conn, old_key)

        if updated_row is None:
            raise PublicationNotFound(str(publication_id))

    return to_public_dict(_row_to_internal_dict(updated_row))
