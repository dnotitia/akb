"""Knowledge Graph service — unified cross-type relation graph.

Supports relations between all resource types: documents, tables, files.
Uses the `edges` table with AKB URI scheme:
  akb://{vault}/doc/{path}
  akb://{vault}/table/{name}
  akb://{vault}/file/{id}

Two auto-extraction sources for documents:
1. Frontmatter fields: depends_on, related_to, implements
2. Markdown links in body: [text](./path/to/doc.md) or akb:// URIs
"""

from __future__ import annotations

import json
import logging
import re
import uuid

from app.db.postgres import get_pool
from app.services.uri_service import parse_uri, doc_uri, table_uri, file_uri

logger = logging.getLogger("akb.graph")

# Matches markdown links: [text](target)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
# Matches akb:// URIs anywhere in text
_AKB_URI_RE = re.compile(r"akb://[^\s\)>`]+")


# ── Link extraction from markdown body ───────────────────────

def extract_markdown_links(content: str) -> list[str]:
    """Extract internal document references from markdown links.

    Returns list of targets (paths, doc IDs, or akb:// URIs).
    Filters out external URLs (http/https/mailto).
    """
    targets: list[str] = []
    seen: set[str] = set()

    for match in _MD_LINK_RE.finditer(content):
        target = match.group(2).strip()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        if target.startswith("akb://"):
            if target not in seen:
                targets.append(target)
                seen.add(target)
            continue
        # Normalize relative paths
        target = target.lstrip("./")
        if "#" in target:
            target = target.split("#")[0]
        if target and target not in seen:
            targets.append(target)
            seen.add(target)

    # Also find bare akb:// URIs in body (not inside markdown links)
    for match in _AKB_URI_RE.finditer(content):
        uri = match.group(0)
        if uri not in seen:
            targets.append(uri)
            seen.add(uri)

    return targets


# ── Store edges (frontmatter + body links) ────────────────────

async def store_document_relations(
    conn,
    vault_id: uuid.UUID,
    vault_name: str,
    doc_path: str,
    depends_on: list[str],
    related_to: list[str],
    implements: list[str],
    body_content: str,
) -> int:
    """Store all edges from a document: frontmatter fields + markdown body links.

    Returns total number of edges stored.
    """
    source = doc_uri(vault_name, doc_path)

    # Delete old edges from this source
    await conn.execute("DELETE FROM edges WHERE source_uri = $1", source)

    count = 0

    for target_ref in depends_on:
        if await _store_edge(conn, vault_id, vault_name, source, "doc", target_ref, "depends_on"):
            count += 1

    for target_ref in related_to:
        if await _store_edge(conn, vault_id, vault_name, source, "doc", target_ref, "related_to"):
            count += 1

    for target_ref in implements:
        if await _store_edge(conn, vault_id, vault_name, source, "doc", target_ref, "implements"):
            count += 1

    # Body markdown links
    body_links = extract_markdown_links(body_content)
    for target_ref in body_links:
        if await _store_edge(conn, vault_id, vault_name, source, "doc", target_ref, "links_to"):
            count += 1

    if count > 0:
        logger.info("Stored %d edges for %s", count, source)

    return count


async def delete_resource_edges(conn, resource_uri: str) -> None:
    """Remove all edges where this resource is source or target."""
    await conn.execute(
        "DELETE FROM edges WHERE source_uri = $1 OR target_uri = $1",
        resource_uri,
    )


async def delete_document_relations(conn, vault_name: str, doc_path: str) -> None:
    """Remove all edges involving a document (by vault name and path)."""
    uri = doc_uri(vault_name, doc_path)
    await delete_resource_edges(conn, uri)


# ── Explicit link/unlink (agent-driven) ───────────────────────

async def link_resources(
    vault_name: str,
    source_uri: str,
    target_uri: str,
    relation_type: str,
    created_by: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Create an explicit edge between any two resources."""
    source_parsed = parse_uri(source_uri)
    target_parsed = parse_uri(target_uri)
    if not source_parsed:
        return {"error": f"Invalid source URI: {source_uri}"}
    if not target_parsed:
        return {"error": f"Invalid target URI: {target_uri}"}

    source_vault_name, source_type, source_id = source_parsed
    target_vault_name, target_type, target_id = target_parsed

    if source_uri == target_uri:
        return {"error": "Cannot link a resource to itself"}

    pool = await get_pool()
    async with pool.acquire() as conn:
        # The edge is owned by the caller-specified vault (source convention),
        # but each endpoint is validated against its own vault from the URI.
        vault = await conn.fetchrow("SELECT id FROM vaults WHERE name = $1", vault_name)
        if not vault:
            return {"error": f"Vault not found: {vault_name}"}
        vault_id = vault["id"]

        source_vault = await conn.fetchrow("SELECT id FROM vaults WHERE name = $1", source_vault_name)
        if not source_vault:
            return {"error": f"Source vault not found: {source_vault_name}"}
        target_vault = await conn.fetchrow("SELECT id FROM vaults WHERE name = $1", target_vault_name)
        if not target_vault:
            return {"error": f"Target vault not found: {target_vault_name}"}

        if not await _resource_exists(conn, source_vault["id"], source_type, source_id):
            return {"error": f"Source resource not found: {source_uri}"}
        if not await _resource_exists(conn, target_vault["id"], target_type, target_id):
            return {"error": f"Target resource not found: {target_uri}"}

        await conn.execute(
            """
            INSERT INTO edges (id, vault_id, source_uri, target_uri, relation_type,
                               source_type, target_type, metadata, created_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (source_uri, target_uri, relation_type) DO UPDATE
            SET metadata = $8, created_by = $9
            """,
            uuid.uuid4(), vault_id, source_uri, target_uri, relation_type,
            source_type, target_type, json.dumps(metadata or {}), created_by,
        )

    logger.info("Linked %s → %s (%s)", source_uri, target_uri, relation_type)
    return {"linked": True, "source": source_uri, "target": target_uri, "relation": relation_type}


async def unlink_resources(
    source_uri: str,
    target_uri: str,
    relation_type: str | None = None,
) -> dict:
    """Remove an edge between two resources."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if relation_type:
            result = await conn.execute(
                "DELETE FROM edges WHERE source_uri = $1 AND target_uri = $2 AND relation_type = $3",
                source_uri, target_uri, relation_type,
            )
        else:
            result = await conn.execute(
                "DELETE FROM edges WHERE source_uri = $1 AND target_uri = $2",
                source_uri, target_uri,
            )

    count = int(result.split(" ")[1]) if " " in result else 0
    logger.info("Unlinked %s → %s (%d removed)", source_uri, target_uri, count)
    return {"unlinked": count, "source": source_uri, "target": target_uri}


# ── Graph queries ─────────────────────────────────────────────

async def get_resource_relations(
    vault: str,
    resource_uri: str,
    *,
    vault_id: uuid.UUID,
    direction: str = "both",
    relation_type: str | None = None,
) -> list[dict]:
    """Get direct (1-hop) relations for any resource.

    `vault_id` scopes both the edge fetch and the name resolution to a
    single vault. Cross-vault edges (an edge whose endpoint URI lives in
    another vault) are returned only as opaque URIs — the endpoint name
    is NOT resolved unless the resource also exists in this vault. This
    is the security boundary: a caller authorized for vault A cannot
    learn the title/description/filename of a doc/table/file in vault B
    even if A holds an edge pointing at B.
    """
    pool = await get_pool()
    results = []
    uris_to_resolve: list[tuple[str, str]] = []

    async with pool.acquire() as conn:
        if direction in ("outgoing", "both"):
            base_params: list = [resource_uri, vault_id]
            if relation_type:
                rows = await conn.fetch(
                    """
                    SELECT e.relation_type, e.target_uri, e.target_type
                    FROM edges e
                    WHERE e.source_uri = $1 AND e.vault_id = $2 AND e.relation_type = $3
                    """,
                    *base_params, relation_type,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT e.relation_type, e.target_uri, e.target_type
                    FROM edges e
                    WHERE e.source_uri = $1 AND e.vault_id = $2
                    """,
                    *base_params,
                )
            for r in rows:
                results.append({
                    "direction": "outgoing",
                    "relation": r["relation_type"],
                    "uri": r["target_uri"],
                    "resource_type": r["target_type"],
                })
                uris_to_resolve.append((r["target_uri"], r["target_type"]))

        if direction in ("incoming", "both"):
            base_params = [resource_uri, vault_id]
            if relation_type:
                rows = await conn.fetch(
                    """
                    SELECT e.relation_type, e.source_uri, e.source_type
                    FROM edges e
                    WHERE e.target_uri = $1 AND e.vault_id = $2 AND e.relation_type = $3
                    """,
                    *base_params, relation_type,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT e.relation_type, e.source_uri, e.source_type
                    FROM edges e
                    WHERE e.target_uri = $1 AND e.vault_id = $2
                    """,
                    *base_params,
                )
            for r in rows:
                results.append({
                    "direction": "incoming",
                    "relation": r["relation_type"],
                    "uri": r["source_uri"],
                    "resource_type": r["source_type"],
                })
                uris_to_resolve.append((r["source_uri"], r["source_type"]))

        names = await _batch_resolve_names(conn, uris_to_resolve, vault_id=vault_id)
        for entry in results:
            name = names.get(entry["uri"])
            if name:
                entry["name"] = name

    return results


async def get_graph(
    vault: str,
    resource_uri: str | None = None,
    depth: int = 2,
    limit: int = 50,
    *,
    vault_id: uuid.UUID | None = None,
) -> dict:
    """Get a subgraph centered on a resource, or the full vault graph.

    Returns { nodes: [...], edges: [...] } suitable for visualization.
    Nodes include all resource types (doc, table, file).

    `vault_id` scopes the graph to one vault: both the BFS edge fetch
    and the name resolution refuse to look outside it. Caller must
    have verified reader access on this vault. When `vault_id` is
    omitted we look it up by name as a convenience for legacy
    callers — but the auth check is still the caller's responsibility.
    """
    pool = await get_pool()
    nodes: dict[str, dict] = {}
    edge_list: list[dict] = []

    async with pool.acquire() as conn:
        if vault_id is None:
            vault_row = await conn.fetchrow("SELECT id FROM vaults WHERE name = $1", vault)
            if not vault_row:
                return {"nodes": [], "edges": []}
            vault_id = vault_row["id"]

        if resource_uri:
            await _bfs_collect(conn, vault_id, vault, resource_uri, depth, limit, nodes, edge_list)
        else:
            edge_rows = await conn.fetch(
                """
                SELECT source_uri, target_uri, source_type, target_type, relation_type
                FROM edges WHERE vault_id = $1
                LIMIT $2
                """,
                vault_id, limit * 3,
            )

            uris_to_resolve: list[tuple[str, str]] = []
            for r in edge_rows:
                uris_to_resolve.append((r["source_uri"], r["source_type"]))
                uris_to_resolve.append((r["target_uri"], r["target_type"]))
                edge_list.append({
                    "source": r["source_uri"],
                    "target": r["target_uri"],
                    "relation": r["relation_type"],
                })

            # Resolve names within this vault only — cross-vault endpoints
            # come back as the URI string itself (no title/description leak).
            # Every URI referenced by an edge MUST still become a node so
            # the visualization doesn't get orphan source/target IDs.
            names = await _batch_resolve_names(conn, uris_to_resolve, vault_id=vault_id)
            for uri, rtype in uris_to_resolve:
                if uri not in nodes:
                    nodes[uri] = {
                        "uri": uri,
                        "resource_type": rtype,
                        "name": names.get(uri) or uri,
                    }

    return {
        "nodes": list(nodes.values()),
        "edges": edge_list,
    }


async def get_provenance(doc_id: str, *, vault_id: uuid.UUID) -> dict:
    """Get provenance info for a document.

    `vault_id` scopes both the document lookup and the relation fetch.
    Caller must have verified reader access on this vault — without
    the scope a doc_id alone would let anyone read another vault's
    document title/path/author/commit metadata.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT d.id, d.title, d.path, d.created_by, d.created_at, d.updated_at,
                   d.current_commit, d.metadata, v.name as vault_name
            FROM documents d
            JOIN vaults v ON d.vault_id = v.id
            WHERE (d.id::text = $1 OR d.metadata->>'id' = $1) AND d.vault_id = $2
            """,
            doc_id, vault_id,
        )
        if not row:
            return {"error": "Document not found"}

        uri = doc_uri(row["vault_name"], row["path"])
        relations = await get_resource_relations(row["vault_name"], uri, vault_id=vault_id)

        return {
            "doc_id": str(row["id"]),
            "title": row["title"],
            "path": row["path"],
            "vault": row["vault_name"],
            "uri": uri,
            "created_by": row["created_by"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            "current_commit": row["current_commit"],
            "relations": relations,
        }


# ── Batch name resolution ────────────────────────────────────

async def _batch_resolve_names(
    conn, uris: list[tuple[str, str]],
    *,
    vault_id: uuid.UUID,
) -> dict[str, str]:
    """Resolve multiple URIs to display names in batch (3 queries max, not N).

    Scoped to `vault_id`. URIs whose underlying resource lives in a
    different vault won't get a resolved title/description/name —
    they fall back to the identifier from the URI itself. This is a
    privacy boundary: callers authorized for one vault shouldn't learn
    the human-readable names of resources in another vault, even if
    they can see the raw URI through a cross-vault edge.
    """
    if not uris:
        return {}

    doc_uris: dict[str, str] = {}
    table_uris: dict[str, str] = {}
    file_uris: dict[str, str] = {}

    doc_paths: list[str] = []
    table_names: list[str] = []
    file_ids: list[str] = []

    for uri, rtype in uris:
        parsed = parse_uri(uri)
        if not parsed:
            continue
        _, _, identifier = parsed
        if rtype == "doc":
            doc_paths.append(identifier)
            doc_uris[identifier] = uri
        elif rtype == "table":
            table_names.append(identifier)
            table_uris[identifier] = uri
        elif rtype == "file":
            file_ids.append(identifier)
            file_uris[identifier] = uri

    names: dict[str, str] = {}

    if doc_paths:
        rows = await conn.fetch(
            "SELECT path, title FROM documents "
            "WHERE path = ANY($1::text[]) AND vault_id = $2",
            doc_paths, vault_id,
        )
        for r in rows:
            uri = doc_uris.get(r["path"])
            if uri:
                names[uri] = r["title"]
        # Fallback for unresolved (cross-vault or genuinely missing): use
        # the path/identifier so the visualization still renders something.
        for path, uri in doc_uris.items():
            if uri not in names:
                names[uri] = path

    if table_names:
        rows = await conn.fetch(
            "SELECT name, description FROM vault_tables "
            "WHERE name = ANY($1::text[]) AND vault_id = $2",
            table_names, vault_id,
        )
        for r in rows:
            uri = table_uris.get(r["name"])
            if uri:
                names[uri] = r["description"] if r["description"] else r["name"]
        for tname, uri in table_uris.items():
            if uri not in names:
                names[uri] = tname

    if file_ids:
        rows = await conn.fetch(
            "SELECT id::text, name FROM vault_files "
            "WHERE id::text = ANY($1::text[]) AND vault_id = $2",
            file_ids, vault_id,
        )
        for r in rows:
            uri = file_uris.get(r["id"])
            if uri:
                names[uri] = r["name"]
        for fid, uri in file_uris.items():
            if uri not in names:
                names[uri] = fid

    return names


# ── Helpers ───────────────────────────────────────────────────

async def _resource_exists(conn, vault_id: uuid.UUID, rtype: str, identifier: str) -> bool:
    """Check if a resource actually exists in the database."""
    if rtype == "doc":
        return bool(await conn.fetchval(
            "SELECT 1 FROM documents WHERE vault_id = $1 AND path = $2", vault_id, identifier,
        ))
    elif rtype == "table":
        return bool(await conn.fetchval(
            "SELECT 1 FROM vault_tables WHERE vault_id = $1 AND name = $2", vault_id, identifier,
        ))
    elif rtype == "file":
        return bool(await conn.fetchval(
            "SELECT 1 FROM vault_files WHERE vault_id = $1 AND id::text = $2", vault_id, identifier,
        ))
    return False


async def _store_edge(
    conn, vault_id: uuid.UUID, vault_name: str,
    source_uri: str, source_type: str,
    target_ref: str, relation_type: str,
) -> bool:
    """Resolve target reference and insert edge. Returns True if stored."""
    # If target is already an akb:// URI, parse it directly
    parsed = parse_uri(target_ref)
    if parsed:
        _, target_type, _ = parsed
        target_uri = target_ref
    else:
        # Legacy: resolve as doc ref within the same vault
        target_id = await _resolve_doc_ref(conn, vault_id, target_ref)
        if not target_id:
            return False
        target_path = await conn.fetchval("SELECT path FROM documents WHERE id = $1", target_id)
        if not target_path:
            return False
        target_uri = doc_uri(vault_name, target_path)
        target_type = "doc"

    if source_uri == target_uri:
        return False

    await conn.execute(
        """
        INSERT INTO edges (id, vault_id, source_uri, target_uri, relation_type,
                           source_type, target_type)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT DO NOTHING
        """,
        uuid.uuid4(), vault_id, source_uri, target_uri, relation_type,
        source_type, target_type,
    )
    return True


async def _resolve_doc_ref(conn, vault_id: uuid.UUID, ref: str) -> uuid.UUID | None:
    """Resolve a document reference (doc_id, path, or metadata.id) to UUID."""
    # Full match pattern: UUID, metadata.id, or path substring
    row = await conn.fetchrow(
        "SELECT id FROM documents WHERE vault_id = $1 AND (id::text = $2 OR metadata->>'id' = $2 OR path LIKE '%' || $2 || '%')",
        vault_id, ref,
    )
    if row:
        return row["id"]

    # By UUID
    try:
        uid = uuid.UUID(ref)
        row = await conn.fetchrow(
            "SELECT id FROM documents WHERE vault_id = $1 AND id = $2",
            vault_id, uid,
        )
        if row:
            return row["id"]
    except ValueError:
        pass

    # By path (exact or suffix match)
    row = await conn.fetchrow(
        "SELECT id FROM documents WHERE vault_id = $1 AND path = $2",
        vault_id, ref,
    )
    if row:
        return row["id"]

    # By path suffix
    row = await conn.fetchrow(
        "SELECT id FROM documents WHERE vault_id = $1 AND path LIKE '%/' || $2",
        vault_id, ref,
    )
    if row:
        return row["id"]

    return None


async def _bfs_collect(
    conn, vault_id: uuid.UUID, vault_name: str, start_uri: str,
    depth: int, limit: int,
    nodes: dict[str, dict], edges: list[dict],
) -> None:
    """BFS traversal from a starting resource URI, scoped to one vault.

    Both edge fetches gate on `edges.vault_id` so the traversal cannot
    follow cross-vault links into vaults the caller wasn't authorized
    on. Endpoints whose URI lives in another vault still appear as
    leaf nodes (with the URI as the only signal) but are never used as
    seeds for the next BFS layer's name resolution.
    """
    queue = [start_uri]
    visited: set[str] = set()

    for current_depth in range(depth + 1):
        if not queue or len(nodes) >= limit:
            break

        next_queue: list[str] = []

        unvisited = [u for u in queue if u not in visited]
        if not unvisited:
            break

        outgoing = await conn.fetch(
            "SELECT source_uri, target_uri, target_type, relation_type "
            "FROM edges WHERE source_uri = ANY($1::text[]) AND vault_id = $2",
            unvisited, vault_id,
        )
        incoming = await conn.fetch(
            "SELECT source_uri, source_type, target_uri, relation_type "
            "FROM edges WHERE target_uri = ANY($1::text[]) AND vault_id = $2",
            unvisited, vault_id,
        )

        # Index edges by source/target for quick lookup
        out_by_uri: dict[str, list] = {}
        for r in outgoing:
            out_by_uri.setdefault(r["source_uri"], []).append(r)
        in_by_uri: dict[str, list] = {}
        for r in incoming:
            in_by_uri.setdefault(r["target_uri"], []).append(r)

        for uri in unvisited:
            if uri in visited or len(nodes) >= limit:
                continue
            visited.add(uri)

            parsed = parse_uri(uri)
            if not parsed:
                continue
            _, rtype, _ = parsed

            # Placeholder node (name resolved in batch later)
            nodes[uri] = {
                "uri": uri,
                "resource_type": rtype,
                "name": uri,  # placeholder
                "depth": current_depth,
            }

            if current_depth >= depth:
                continue

            for r in out_by_uri.get(uri, []):
                edges.append({
                    "source": uri,
                    "target": r["target_uri"],
                    "relation": r["relation_type"],
                })
                if r["target_uri"] not in visited:
                    next_queue.append(r["target_uri"])

            for r in in_by_uri.get(uri, []):
                edges.append({
                    "source": r["source_uri"],
                    "target": uri,
                    "relation": r["relation_type"],
                })
                if r["source_uri"] not in visited:
                    next_queue.append(r["source_uri"])

        queue = next_queue

    uris_to_resolve = [(uri, n["resource_type"]) for uri, n in nodes.items()]
    names = await _batch_resolve_names(conn, uris_to_resolve, vault_id=vault_id)
    for uri, node in nodes.items():
        node["name"] = names.get(uri) or uri


# ── URI builder helpers for callers ──────────────────────────

async def resolve_doc_to_uri(vault_name: str, doc_ref: str) -> str | None:
    """Resolve a doc ref (doc_id, path, metadata.id) to its akb:// URI."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        vault = await conn.fetchrow("SELECT id FROM vaults WHERE name = $1", vault_name)
        if not vault:
            return None
        doc_id = await _resolve_doc_ref(conn, vault["id"], doc_ref)
        if not doc_id:
            return None
        path = await conn.fetchval("SELECT path FROM documents WHERE id = $1", doc_id)
        if not path:
            return None
        return doc_uri(vault_name, path)
