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
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import frontmatter

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import AKBError, NotFoundError
from app.services import file_service, table_service
from app.services.document_service import DocumentService

logger = logging.getLogger("akb.publications")


# Frozen at import — AKB_PUBLIC_BASE_URL is env-only, so there's no runtime
# toggle to handle. Trailing slash normalized once here instead of per row.
_PUBLIC_BASE: str | None = settings.public_base_url.rstrip("/") or None


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
    ALL = ("live", "snapshot")

# Singleton DocumentService — cheap to instantiate but holds a Git client
# we don't want to recreate on every request.
_doc_service: DocumentService | None = None


def _get_doc_service() -> DocumentService:
    global _doc_service
    if _doc_service is None:
        _doc_service = DocumentService()
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


def _publication_row_to_dict(row) -> dict | None:
    """Serialize an asyncpg publications row to a JSON-friendly dict.

    Normalizations applied:
    - `id` is renamed to `publication_id` so callers never confuse it
      with the underlying resource id (document_id / file_id).
    - `query_params` is parsed from JSON string into a dict.
    - UUID columns become strings.
    - Datetime columns become ISO strings.

    Returns None only if `row` is None. Otherwise the result is guaranteed
    to contain `publication_id`, `slug`, and `resource_type`. Raises
    PublicationError if `query_params` JSON is corrupted (data integrity).
    """
    if row is None:
        return None
    d = dict(row)
    for k, v in list(d.items()):
        if isinstance(v, uuid.UUID):
            d[k] = str(v)
        elif hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    if "query_params" in d and isinstance(d["query_params"], str):
        try:
            d["query_params"] = json.loads(d["query_params"])
        except (json.JSONDecodeError, TypeError) as e:
            slug_or_id = d.get("slug") or d.get("id") or "?"
            raise PublicationError(
                f"Corrupt query_params JSON for publication {slug_or_id}: {e}",
                status_code=500,
            )
    if "id" in d:
        d["publication_id"] = d.pop("id")
    return d


def to_uuid(value) -> uuid.UUID:
    """Coerce a string or UUID into a uuid.UUID. Public helper used by routes."""
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


# Backwards-compat alias (private name removed in next pass)
_to_uuid = to_uuid


# ============================================================
# CRUD
# ============================================================

async def create_publication(
    *,
    vault_id: uuid.UUID,
    resource_type: str,
    document_id: uuid.UUID | None = None,
    file_id: uuid.UUID | None = None,
    query_sql: str | None = None,
    query_vault_names: list[str] | None = None,
    query_params: dict | None = None,
    password: str | None = None,
    max_views: int | None = None,
    expires_at: datetime | None = None,
    title: str | None = None,
    mode: str = "live",
    section_filter: str | None = None,
    allow_embed: bool = True,
    created_by: uuid.UUID | None = None,
) -> dict:
    """Create a publication row. Returns the publication dict including slug + public URL.

    All validation lives here — routes are thin adapters that pass parsed
    arguments. Raises ValueError for any invalid input.
    """
    if resource_type not in ResourceType.ALL:
        raise ValueError(f"Invalid resource_type: {resource_type}")
    if mode not in Mode.ALL:
        raise ValueError(f"Invalid mode: {mode}")

    # Validate that the right resource fields are present
    if resource_type == ResourceType.DOCUMENT and not document_id:
        raise ValueError("document_id is required for resource_type='document'")
    if resource_type == ResourceType.FILE and not file_id:
        raise ValueError("file_id is required for resource_type='file'")
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
    pwd_hash = _hash_password(password) if password else None

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO publications (
                slug, vault_id, resource_type, document_id, file_id,
                query_sql, query_vault_names, query_params,
                password_hash, max_views, expires_at,
                mode, section_filter, allow_embed, title, created_by
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
            RETURNING id, slug, vault_id, resource_type, document_id, file_id,
                      query_sql, query_vault_names, query_params, max_views,
                      view_count, expires_at, mode, section_filter, allow_embed,
                      title, created_at
            """,
            slug, vault_id, resource_type, document_id, file_id,
            query_sql, query_vault_names, json.dumps(query_params or {}),
            pwd_hash, max_views, expires_at,
            mode, section_filter, allow_embed, title, created_by,
        )

    result = _enrich_publication(_publication_row_to_dict(row)) or {}
    result["password_protected"] = pwd_hash is not None
    logger.info("Publication created: %s (type=%s)", slug, resource_type)
    return result


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
    mode: str = "live",
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

    document_uuid: uuid.UUID | None = None
    file_uuid: uuid.UUID | None = None
    resolved_query_vaults = query_vault_names

    if resource_type == ResourceType.DOCUMENT:
        if not doc_id:
            raise ValueError("doc_id required for resource_type='document'")
        from app.repositories.document_repo import DocumentRepository
        doc_repo = DocumentRepository(pool)
        async with pool.acquire() as conn:
            doc_row = await doc_repo.find_by_ref_with_conn(conn, vault_id, doc_id)
        if not doc_row:
            raise ValueError(f"Document not found: {doc_id}")
        document_uuid = doc_row["id"]
    elif resource_type == ResourceType.FILE:
        if not file_id:
            raise ValueError("file_id required for resource_type='file'")
        try:
            file_uuid = uuid.UUID(file_id)
        except ValueError:
            raise ValueError("Invalid file_id format")
    elif resource_type == ResourceType.TABLE_QUERY:
        if not resolved_query_vaults:
            resolved_query_vaults = [vault_name]
    else:
        raise ValueError(f"Invalid resource_type: {resource_type}")

    expires_at = parse_expires_in(expires_in)

    return await create_publication(
        vault_id=vault_id,
        resource_type=resource_type,
        document_id=document_uuid,
        file_id=file_uuid,
        query_sql=query_sql,
        query_vault_names=resolved_query_vaults,
        query_params=query_params,
        password=password,
        max_views=max_views,
        expires_at=expires_at,
        title=title,
        mode=mode,
        section_filter=section_filter,
        allow_embed=allow_embed,
        created_by=created_by,
    )


async def delete_publication(*, publication_id: uuid.UUID | None = None, slug: str | None = None) -> bool:
    """Delete a publication by id or slug. Returns True if deleted."""
    if not publication_id and not slug:
        raise ValueError("Either publication_id or slug must be provided")

    pool = await get_pool()
    async with pool.acquire() as conn:
        if publication_id:
            result = await conn.execute("DELETE FROM publications WHERE id = $1", publication_id)
        else:
            result = await conn.execute("DELETE FROM publications WHERE slug = $1", slug)
    deleted = result.endswith(" 1")
    if deleted:
        logger.info("Publication deleted: %s", publication_id or slug)
    return deleted


async def delete_publications_for_document(document_id: uuid.UUID) -> int:
    """Delete all publications for a given document. Returns count."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "DELETE FROM publications WHERE document_id = $1 RETURNING id",
            document_id,
        )
    return len(rows)


# Columns selected for list/inspection queries (excludes large/sensitive fields).
_PUBLICATION_LIST_COLUMNS = """
    id, slug, vault_id, resource_type, document_id, file_id, title,
    query_params, max_views, view_count, expires_at, mode, snapshot_at,
    section_filter, allow_embed,
    password_hash IS NOT NULL AS password_protected,
    created_at
"""


def _enrich_publication(d: dict | None) -> dict | None:
    """Add URL fields to a publication dict. No-op for None.

    Sets `public_url` (relative) unconditionally; `public_url_full` and
    `public_base` are populated iff `AKB_PUBLIC_BASE_URL` is set, else None.
    """
    if d is None:
        return None
    slug = d.get("slug")
    if not slug:
        raise PublicationError(
            "Publication row missing 'slug' — data integrity error",
            status_code=500,
        )
    d["public_url"] = f"/p/{slug}"
    d["public_base"] = _PUBLIC_BASE
    d["public_url_full"] = f"{_PUBLIC_BASE}/p/{slug}" if _PUBLIC_BASE else None
    return d


async def list_publications(vault_id: uuid.UUID, resource_type: str | None = None) -> list[dict]:
    """List active publications for a vault."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if resource_type:
            rows = await conn.fetch(
                f"SELECT {_PUBLICATION_LIST_COLUMNS} FROM publications "
                "WHERE vault_id = $1 AND resource_type = $2 "
                "ORDER BY created_at DESC",
                vault_id, resource_type,
            )
        else:
            rows = await conn.fetch(
                f"SELECT {_PUBLICATION_LIST_COLUMNS} FROM publications "
                "WHERE vault_id = $1 "
                "ORDER BY created_at DESC",
                vault_id,
            )
    return [_enrich_publication(_publication_row_to_dict(r)) for r in rows]


async def get_publication_by_slug(slug: str) -> dict | None:
    """Read publication by slug without enforcement (for inspection)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_PUBLICATION_LIST_COLUMNS} FROM publications WHERE slug = $1",
            slug,
        )
    if row is None:
        return None
    return _enrich_publication(_publication_row_to_dict(row))


# ============================================================
# Resolution + Access checks
# ============================================================

async def resolve_publication(
    slug: str,
    password: str | None = None,
    increment_view: bool = True,
    bypass_password: bool = False,
) -> dict:
    """Look up a publication and enforce access controls.

    Raises:
        PublicationNotFound — slug doesn't exist
        PublicationExpired — past expires_at
        PublicationViewLimitReached — view_count >= max_views
        PublicationPasswordRequired — password set but not provided
        PublicationPasswordInvalid — wrong password

    Returns the publication row dict (including vault_name and query_params parsed
    as a dict). Always includes 'publication_id' (never 'id').

    bypass_password: skip the password check (used when caller has already
    verified an HMAC session token at the route layer).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT s.id, s.slug, s.vault_id, s.resource_type, s.document_id, s.file_id,
                   s.query_sql, s.query_vault_names, s.query_params,
                   s.password_hash, s.max_views, s.view_count, s.expires_at,
                   s.mode, s.snapshot_s3_key, s.snapshot_at,
                   s.section_filter, s.allow_embed, s.title, s.created_at,
                   v.name AS vault_name
            FROM publications s
            JOIN vaults v ON s.vault_id = v.id
            WHERE s.slug = $1
            """,
            slug,
        )
        if row is None:
            raise PublicationNotFound(slug)

        if row["expires_at"] is not None and row["expires_at"] <= datetime.now(timezone.utc):
            raise PublicationExpired()

        if row["max_views"] is not None and row["view_count"] >= row["max_views"]:
            raise PublicationViewLimitReached()

        if row["password_hash"] and not bypass_password:
            if not password:
                raise PublicationPasswordRequired()
            if not _verify_password(password, row["password_hash"]):
                raise PublicationPasswordInvalid()

        if increment_view:
            await conn.execute(
                "UPDATE publications SET view_count = view_count + 1 WHERE id = $1",
                row["id"],
            )

    return _enrich_publication(_publication_row_to_dict(row))


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

    pool = await get_pool()
    async with pool.acquire() as conn:
        doc_row = await conn.fetchrow(
            """
            SELECT d.path, d.title, d.doc_type, d.status, d.summary, d.domain,
                   d.created_by, d.created_at, d.updated_at, d.tags,
                   v.name AS vault_name
            FROM documents d
            JOIN vaults v ON d.vault_id = v.id
            WHERE d.id = $1
            """,
            to_uuid(publication["document_id"]),
        )
        if doc_row is None:
            raise NotFoundError("Document", str(publication["document_id"]))

    body = ""
    content_unavailable = False
    try:
        raw = _get_doc_service().git.read_file(doc_row["vault_name"], doc_row["path"])
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
        "status": doc_row["status"],
        "summary": doc_row["summary"],
        "domain": doc_row["domain"],
        "created_by": doc_row["created_by"],
        "created_at": doc_row["created_at"].isoformat() if doc_row["created_at"] else None,
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

_FILE_PRESIGN_TTL = 300  # 5 minutes — short for security


async def resolve_file_publication(publication: dict) -> dict:
    """Get file metadata + presigned URL (for inline preview of images/PDFs
    rendered via <img>/<embed>). Force-download flows go through
    `get_file_storage_for_publication` and stream via the backend instead.
    """
    if publication["resource_type"] != ResourceType.FILE:
        raise PublicationError("Not a file publication", status_code=400)

    pool = await get_pool()
    async with pool.acquire() as conn:
        file_row = await conn.fetchrow(
            """
            SELECT f.name, f.s3_key, f.mime_type, f.size_bytes, f.collection
            FROM vault_files f
            WHERE f.id = $1
            """,
            to_uuid(publication["file_id"]),
        )
        if file_row is None:
            raise NotFoundError("File", str(publication["file_id"]))

    # Override stored Content-Type with DB value so the browser inline-renders
    # correctly even when the S3 object was uploaded as octet-stream (legacy
    # proxy versions before v0.5.1 didn't propagate mime_type).
    mime = file_row["mime_type"]
    override = mime if mime and mime != "application/octet-stream" else None
    presigned_url = file_service.get_presigned_download_url(
        file_row["s3_key"],
        ttl=_FILE_PRESIGN_TTL,
        response_content_type=override,
    )

    return {
        "resource_type": ResourceType.FILE,
        "name": file_row["name"],
        "title": publication.get("title") or file_row["name"],
        "mime_type": file_row["mime_type"],
        "size_bytes": file_row["size_bytes"],
        "collection": file_row["collection"],
        "download_url": presigned_url,
        "url_expires_in": _FILE_PRESIGN_TTL,
    }


async def get_file_storage_for_publication(publication: dict) -> dict:
    """Return s3_key + minimal metadata needed to stream a file publication
    through the backend. Internal use only — never serialize the result to
    clients (s3_key would leak the storage layout).
    """
    if publication["resource_type"] != ResourceType.FILE:
        raise PublicationError("Not a file publication", status_code=400)

    pool = await get_pool()
    async with pool.acquire() as conn:
        file_row = await conn.fetchrow(
            "SELECT name, s3_key, mime_type, size_bytes FROM vault_files WHERE id = $1",
            to_uuid(publication["file_id"]),
        )
    if file_row is None:
        raise NotFoundError("File", str(publication["file_id"]))

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
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, uuid.UUID):
        return str(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


async def resolve_table_query_publication(publication: dict, url_params: dict | None = None) -> dict:
    """Execute a canned table query publication with URL parameter binding."""
    if publication["resource_type"] != ResourceType.TABLE_QUERY:
        raise PublicationError("Not a table query publication", status_code=400)

    # Snapshot mode: return cached snapshot from S3 (P4)
    if publication.get("mode") == Mode.SNAPSHOT and publication.get("snapshot_s3_key"):
        return await _read_snapshot(publication)

    sql = publication["query_sql"]
    param_defs = publication.get("query_params") or {}
    vault_names = list(publication.get("query_vault_names") or [publication["vault_name"]])
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

        try:
            async with conn.transaction():
                await conn.execute("SET TRANSACTION READ ONLY")
                rows = await conn.fetch(rewritten, *values)
        except Exception as e:
            msg = str(e)
            if "read-only transaction" in msg:
                raise PublicationError("Write operations are not allowed", status_code=403)
            raise PublicationError(f"Query error: {msg}", status_code=400)

    columns = list(dict(rows[0]).keys()) if rows else []
    result_rows = [{k: _serialize_value(v) for k, v in dict(r).items()} for r in rows]

    return {
        "resource_type": ResourceType.TABLE_QUERY,
        "title": publication.get("title") or "Shared query",
        "columns": columns,
        "rows": result_rows,
        "total": len(result_rows),
        "query_params": param_defs,
        "applied_params": {n: url_params.get(n) for n in param_defs},
        "mode": publication.get("mode", Mode.LIVE),
    }


# ============================================================
# Snapshot (P4)
# ============================================================

async def _read_snapshot(publication: dict) -> dict:
    """Read a snapshotted query result from S3."""
    try:
        body = file_service.get_object_bytes(publication["snapshot_s3_key"])
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise PublicationError(f"Corrupt snapshot data: {e}", status_code=500)
    # file_service.StorageError already inherits from AKBError → propagates as 502.

    if not isinstance(data, dict):
        raise PublicationError("Snapshot data is not a valid object", status_code=500)

    data["snapshot_at"] = publication.get("snapshot_at")
    data["mode"] = "snapshot"
    return data


async def create_snapshot(publication_id: uuid.UUID) -> dict:
    """Execute a table_query publication's SQL once and store result in S3.

    The publication's `mode` is then flipped to 'snapshot' so subsequent visits
    return the cached result instead of re-running the query.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT s.id, s.slug, s.vault_id, s.resource_type, s.query_sql,
                   s.query_vault_names, s.query_params, s.title,
                   v.name AS vault_name
            FROM publications s JOIN vaults v ON s.vault_id = v.id
            WHERE s.id = $1
            """,
            publication_id,
        )
        if row is None:
            raise PublicationNotFound(str(publication_id))

    publication = _publication_row_to_dict(row)
    if publication["resource_type"] != ResourceType.TABLE_QUERY:
        raise PublicationError("Snapshots only supported for table_query publications", status_code=400)

    # Force live execution for the snapshot (regardless of current mode)
    publication_for_exec = {**publication, "mode": Mode.LIVE}
    result = await resolve_table_query_publication(publication_for_exec, {})

    s3_key = f"snapshots/{publication_id}.json"
    try:
        file_service.put_object_bytes(
            s3_key,
            json.dumps(result, ensure_ascii=False).encode("utf-8"),
            content_type="application/json",
        )
    except (file_service.StorageError, IOError) as e:
        raise PublicationError(f"Failed to upload snapshot: {e}", status_code=502)

    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE publications
            SET snapshot_s3_key = $1, snapshot_at = $2, mode = 'snapshot', updated_at = $2
            WHERE id = $3
            """,
            s3_key, now, publication_id,
        )

    return {
        "snapshot_s3_key": s3_key,
        "snapshot_at": now.isoformat(),
        "rows": result.get("total", 0),
    }
