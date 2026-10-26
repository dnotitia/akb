"""Opt-in, keyset-paged browse. Select identities before hydrating bodies.

The cursor is a position, not authorization or a snapshot. Every call still
requires the route/tool's current Vault grant. Concurrent moves/inserts before
that position require a refresh; no exact inventory total is inferred.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import uuid

from app.exceptions import NotFoundError, ValidationError
from app.models.document import BrowseContext, BrowseItem, BrowseResponse
from app.repositories.vault_files_repo import confirmed_file_predicate
from app.services.resource_hash import HASH_ALGORITHM
from app.services.search_filters import ArchiveScope, resolve_archive_scope, status_matches
from app.services.uri_service import coll_uri, doc_uri, file_uri, table_uri, vault_uri
from app.util.text import like_escape, normalize_collection_path
from app.utils import ensure_list

MAX_PAGE_SIZE = 200
MAX_PAGE_BODY_BYTES = 16 * 1024 * 1024
_KINDS = ("collection", "document", "table", "file")


def _decode_cursor(cursor: str | None, scope: str) -> tuple[int, str, uuid.UUID] | None:
    if cursor is None:
        return None
    try:
        if len(cursor) > 32_768:
            raise ValueError
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        version, bound_scope, kind, path, resource = json.loads(raw)
        if type(version) is not int or version != 1 or bound_scope != scope or type(kind) is not int or not 0 <= kind < 4:
            raise ValueError
        if not isinstance(path, str) or not isinstance(resource, str):
            raise ValueError
        path.encode("utf-8")
        if "\x00" in path:
            raise ValueError
        return kind, path, uuid.UUID(resource)
    except (ValueError, TypeError, binascii.Error, UnicodeError, RecursionError) as exc:
        raise ValidationError("Invalid browse cursor; restart from the first page for this scope") from exc


def _encode_cursor(scope: str, row: dict) -> str:
    raw = json.dumps([1, scope, row["kind"], row["path"], str(row["id"])], ensure_ascii=False)
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


async def browse_page(
    service, pool, vault: str, *, native: bool, collection: str | None, depth: int,
    content_type: str, include_hashes: bool, include_archived: bool,
    archive_scope: ArchiveScope | None, page_size: int, cursor: str | None, name_query: str | None = None,
) -> BrowseResponse:
    async with pool.acquire() as conn:
        # The size guard, selected Heads, and payload reads share one snapshot.
        # A concurrent replacement cannot swap in a larger, unbudgeted body.
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            return await _browse_page(
                service, pool, conn, vault, native=native, collection=collection, depth=depth,
                content_type=content_type, include_hashes=include_hashes, include_archived=include_archived,
                archive_scope=archive_scope, page_size=page_size, cursor=cursor, name_query=name_query,
            )


async def _browse_page(
    service, pool, conn, vault: str, *, native: bool, collection: str | None, depth: int,
    content_type: str, include_hashes: bool, include_archived: bool,
    archive_scope: ArchiveScope | None, page_size: int, cursor: str | None, name_query: str | None = None,
) -> BrowseResponse:
    if type(page_size) is not int or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ValidationError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
    if type(depth) is not int or not -1 <= depth <= 2**31 - 1:
        raise ValidationError("depth must be -1 or a non-negative integer")
    if content_type not in ("all", "collections", "resources", "documents", "tables", "files"):
        raise ValidationError("Unsupported browse content_type")
    if name_query is not None and (not isinstance(name_query, str) or len(name_query) > 200 or "\x00" in name_query):
        raise ValidationError("name_query must be at most 200 characters without NUL")
    name_query = name_query.strip() if name_query else ""
    scope = resolve_archive_scope(archive_scope, include_archived)
    prefix = normalize_collection_path(collection) if collection is not None else ""
    vault_row = await conn.fetchrow("SELECT id, description FROM vaults WHERE name = $1", vault)
    if vault_row is None:
        raise NotFoundError("Vault", vault)
    vault_id = vault_row["id"]
    binding = hashlib.sha256(json.dumps(
        [str(vault_id), native, prefix, depth, content_type, scope] + ([name_query.casefold()] if name_query else []),
        ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()
    after = _decode_cursor(cursor, binding)
    if prefix:
        root = await conn.fetchrow(
            "SELECT name, summary FROM collections WHERE vault_id = $1 AND path = $2", vault_id, prefix,
        )
        context = BrowseContext(
            type="collection", uri=coll_uri(vault, prefix), path=prefix,
            name=root["name"] if root else prefix.rsplit("/", 1)[-1],
            summary=root["summary"] if root else None,
        )
        parts = prefix.split("/")
        ancestors = ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]
        names = await conn.fetch(
            "SELECT path, name FROM collections WHERE vault_id = $1 AND path = ANY($2::text[])",
            vault_id, ancestors,
        )
        by_path = {row["path"]: row["name"] for row in names}
        context.ancestry = [{"path": path, "name": by_path.get(path, path.rsplit("/", 1)[-1])} for path in ancestors]
    else:
        context = BrowseContext(
            type="vault", uri=vault_uri(vault), name=vault, path="",
            description=vault_row["description"],
        )

    params: list = [vault_id]
    if name_query:
        params.append("%" + like_escape(name_query) + "%")
    def names_predicate(name: str, path: str) -> str:
        return f" AND ({name} ILIKE $2 ESCAPE '\\' OR {path} ILIKE $2 ESCAPE '\\')" if name_query else ""

    arms = []
    if content_type in ("all", "documents", "collections"):
        arms.append("SELECT 0 AS kind, id, path, 0::bigint AS body_bytes FROM collections WHERE vault_id = $1" + names_predicate("name", "path"))
    if content_type in ("all", "documents", "resources"):
        arms.append(
            "SELECT 1 AS kind, rs.resource_id AS id, rs.current_path AS path, 0::bigint AS body_bytes "
            "FROM native_resources rs "
            "WHERE rs.namespace_id = $1 AND rs.surface = 'document' AND rs.lifecycle = 'live'"
            if native else "SELECT 1 AS kind, id, path, 0::bigint AS body_bytes FROM documents WHERE vault_id = $1" + names_predicate("title", "path")
        )
    if scope != "archived":
        for kind, relation in ((2, "vault_tables"), (3, "vault_files")):
            if content_type not in ("all", "resources", f"{_KINDS[kind]}s"):
                continue
            confirmed = f" AND {confirmed_file_predicate('r')}" if kind == 3 else ""
            arms.append(
                f"SELECT {kind} AS kind, r.id, COALESCE(c.path || '/', '') || r.name AS path, 0::bigint AS body_bytes "
                f"FROM {relation} r LEFT JOIN collections c ON c.id = r.collection_id "
                f"WHERE r.vault_id = $1{confirmed}" + names_predicate("r.name", "COALESCE(c.path || '/', '') || r.name")
            )
    # Native names live in verified revision frontmatter. Never substitute the
    # frozen legacy title or require asynchronous indexing for a fresh rename.
    # Keep scanning bounded identities (including empty match pages), then
    # match canonical names below. Other catalogs can filter before LIMIT.
    if name_query and native and content_type in ("all", "documents", "resources"):
        # The Native document arm doesn't use the name parameter. Reference it
        # even in document-only scopes so PostgreSQL can infer its text type.
        arms = [arm + " AND $2::text IS NOT NULL" for arm in arms]
    clauses = []
    if prefix:
        params.append(like_escape(prefix) + "/%")
        clauses.append(f"path LIKE ${len(params)} ESCAPE '\\'")
    if depth >= 0:
        params.append(depth + (prefix.count("/") + 1 if prefix else 0))
        clauses.append(f"length(path) - length(replace(path, '/', '')) <= ${len(params)}::bigint")
    if after:
        params.extend(after)
        clauses.append(
            f"(kind, path COLLATE \"C\", id) > "
            f"(${len(params) - 2}::int, ${len(params) - 1}::text COLLATE \"C\", ${len(params)}::uuid)"
        )
    where = " AND ".join(clauses) or "TRUE"
    params.append(page_size + 1)
    rows = await conn.fetch(
        f"SELECT kind, id, path, body_bytes FROM ({' UNION ALL '.join(arms)}) entries "
        f"WHERE {where} ORDER BY kind, path COLLATE \"C\", id LIMIT ${len(params)}",
        *params,
    ) if arms else []
    more = len(rows) > page_size
    selected = [dict(row) for row in rows[:page_size]]
    native_ids = [row["id"] for row in selected if row["kind"] == 1] if native else []
    if native_ids:
        # Join revision/manifest metadata only for this bounded identity page,
        # not every Head in the Vault before ORDER BY/LIMIT.
        sizes = await conn.fetch(
            "SELECT rs.resource_id, pm.byte_size FROM native_resources rs "
            "LEFT JOIN native_revisions rv ON rv.resource_id = rs.resource_id AND rv.revision_id = rs.head_revision_id "
            "LEFT JOIN native_payload_manifests pm ON pm.payload_manifest_id = rv.payload_manifest_id "
            "WHERE rs.namespace_id = $1 AND rs.resource_id = ANY($2::uuid[])", vault_id, native_ids,
        )
        by_resource = {row["resource_id"]: row["byte_size"] for row in sizes}
        for row in selected:
            if row["kind"] == 1:
                row["body_bytes"] = by_resource.get(row["id"])
    body_bytes = 0
    for index, row in enumerate(selected):
        size = row["body_bytes"] or 0
        if size > MAX_PAGE_BODY_BYTES:
            raise ValidationError("A Native Document exceeds the browse body budget; retrieve it by its document URI instead")
        if index and body_bytes + size > MAX_PAGE_BODY_BYTES:
            selected = selected[:index]
            more = True
            break
        body_bytes += size
    by_id = await _hydrate(service, pool, conn, vault, vault_id, selected, native=native, include_hashes=include_hashes)
    items = [by_id[(row["kind"], row["id"])] for row in selected if (row["kind"], row["id"]) in by_id]
    items = [item for item in items if item.type != "document" or status_matches(item.status, scope)]
    if name_query and native:
        needle = name_query.casefold()
        items = [item for item in items if item.type != "document"
                 or needle in item.name.casefold() or needle in item.path.casefold()]
    return BrowseResponse(
        vault=vault, path=prefix, archive_scope=scope, context=context, items=items,
        scanned=len(selected), complete=not more,
        name_query=name_query or None,
        next_cursor=_encode_cursor(binding, selected[-1]) if more else None,
        hint=(
            "More identities remain; follow next_cursor even when this page has no visible items. "
            "Use collection and depth=0 to drill down. Counts describe this page, not the whole Vault."
            if more else "Traversal complete for this scope. Refresh to include concurrent moves or inserts before the cursor."
        ),
    )


async def _hydrate(service, pool, conn, vault, vault_id, selected, *, native: bool, include_hashes: bool):
    result: dict[tuple[int, uuid.UUID], BrowseItem] = {}
    for kind, relation in ((0, "collections"), (1, "documents"), (2, "vault_tables"), (3, "vault_files")):
        ids = [row["id"] for row in selected if row["kind"] == kind]
        if not ids or (kind == 1 and native):
            continue
        if kind in (2, 3):
            confirmed = f" AND {confirmed_file_predicate('r')}" if kind == 3 else ""
            rows = await conn.fetch(
                f"SELECT r.*, c.path AS collection FROM {relation} r "
                f"LEFT JOIN collections c ON c.id = r.collection_id "
                f"WHERE r.vault_id = $1 AND r.id = ANY($2::uuid[]){confirmed}", vault_id, ids,
            )
        else:
            rows = await conn.fetch(
                f"SELECT * FROM {relation} WHERE vault_id = $1 AND id = ANY($2::uuid[])", vault_id, ids,
            )
        for row in rows:
            r = dict(row)
            if kind == 0:
                # Stored legacy counters can be stale under Native authority.
                # A bounded page deliberately does not recount an entire Vault.
                item = BrowseItem(
                    type="collection", name=r["name"], path=r["path"], uri=coll_uri(vault, r["path"]),
                    summary=r["summary"], doc_count=None if native else r["doc_count"],
                    last_updated=None if native else r["last_updated"],
                )
            elif kind == 1:
                content_hash, algorithm = r.get("content_hash"), r.get("hash_algorithm")
                if include_hashes and (
                    not content_hash or algorithm != HASH_ALGORITHM
                    or r.get("content_hash_commit") != r.get("current_commit")
                ):
                    import asyncio
                    from app.services.document_service import _parse_markdown

                    raw = await asyncio.to_thread(service.git.read_file, vault, r["path"], r.get("current_commit"))
                    if raw is not None:
                        from app.repositories.document_repo import DocumentRepository

                        _, body = _parse_markdown(raw)
                        content_hash, algorithm = await service._ensure_document_hash(DocumentRepository(pool), r, body, persist=False, conn=conn)
                item = BrowseItem(
                    type="document", name=r["title"], path=r["path"], uri=doc_uri(vault, r["path"]),
                    summary=r["summary"], doc_type=r["doc_type"], status=r["status"], tags=list(r["tags"] or []),
                    last_updated=r["updated_at"], current_commit=r["current_commit"] if include_hashes else None,
                    content_hash=content_hash if include_hashes else None, hash_algorithm=algorithm if include_hashes else None,
                )
            elif kind == 2:
                from app.repositories.table_data_repo import pg_short_name

                item = BrowseItem(
                    type="table", name=r["name"], path=r["name"], collection=r["collection"],
                    uri=table_uri(vault, r["name"], collection=r["collection"]), summary=r["description"],
                    columns=ensure_list(r["columns"]) if isinstance(r["columns"], str) else r["columns"],
                    sql_name=pg_short_name(r["name"]), last_updated=r["created_at"],
                )
            else:
                item = BrowseItem(
                    type="file", name=r["name"], path=(f"{r['collection']}/" if r["collection"] else "") + r["name"],
                    collection=r["collection"], uri=file_uri(vault, str(r["id"]), collection=r["collection"]),
                    mime_type=r["mime_type"], size_bytes=r["size_bytes"], summary=r["description"], last_updated=r["created_at"],
                    content_hash=r.get("content_hash") if include_hashes else None,
                    hash_algorithm=r.get("hash_algorithm") if include_hashes else None,
                    etag=r.get("etag") if include_hashes else None,
                    storage_version=r.get("storage_version") if include_hashes else None,
                    version=(r.get("storage_version") or r.get("etag")) if include_hashes else None,
                )
            result[(kind, r["id"])] = item
    if native:
        ids = [row["id"] for row in selected if row["kind"] == 1]
        if ids:
            items = await service._browse_native_documents(
                vault, vault_id, prefix="", max_depth=-1, include_hashes=include_hashes,
                include_archived=True, resource_ids=ids,
                conn=conn,
            )
            # Native paths are unique among live resources. A concurrent move
            # cannot silently substitute a different identity into this page.
            paths = {row["path"]: row["id"] for row in selected if row["kind"] == 1}
            for item in items:
                if item.path in paths:
                    result[(1, paths[item.path])] = item
    return result
