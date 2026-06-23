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
from itertools import zip_longest
from typing import Literal, get_args

from app.db.postgres import get_pool
from app.services.uri_service import parse_uri, doc_uri, table_uri, file_uri
from app.util.errors import (
    err,
    INVALID_ARGUMENT,
    INVALID_URI,
    NOT_FOUND,
    SELF_LINK,
)

logger = logging.getLogger("akb.graph")

# ── Relation vocabulary — single source of truth ─────────────
#
# The explicit (agent-driven) link/unlink write surface accepts exactly
# these relation types. This is THE definition: the REST request model
# (`app.api.routes.knowledge.RelationType`) and the MCP `akb_link` /
# `akb_unlink` tool inputSchemas (`mcp_server.tools`) both derive from it
# rather than re-spelling the list, so the surfaces cannot drift apart.
#
# NOTE: this is the *user-settable* set. The document edge-extraction
# pipeline additionally stores `links_to` edges (markdown body links —
# see `extract_and_store_links`), which are never set through link/unlink
# and so are intentionally excluded here. A `links_to` edge is removed by
# its document's lifecycle (re-extraction / deletion), or via unlink with
# the relation omitted (bulk remove), not by naming it.
LinkRelationType = Literal[
    "depends_on",
    "related_to",
    "implements",
    "references",
    "attached_to",
    "derived_from",
]
LINK_RELATION_TYPES: tuple[str, ...] = get_args(LinkRelationType)

# Matches markdown links: [text](target)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
# Matches Obsidian-style wikilinks: [[target]] or [[target|alias]]. Only the
# target (before the first '|') is the link; the rest is display text. The
# inner run cannot contain '[' or ']'.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")
# Matches akb:// URIs anywhere in text. Stops at whitespace, ) > ` AND the
# wikilink/alias delimiters [ ] | — so a bare-URI scan over a wikilink like
# `[[akb://…/x.md|Label]]` yields `akb://…/x.md`, not `akb://…/x.md|Label`
# (the bug where the alias's first word got glued onto the target path,
# producing an edge to a non-existent doc that no graph node could match).
_AKB_URI_RE = re.compile(r"akb://[^\s\)\]\[\|>`]+")
# Code spans — fenced blocks (``` / ~~~) and inline (`…`). Anything inside
# is example/snippet text, NOT a real link: a session report quoting
# `akb://project-akb/...`, a regex example `akb://\1/coll/\2/doc/\3`, or a
# JSON fragment `"incidents/a.md"` must NOT become a graph edge to a
# (usually non-existent) target. Strip code before scanning for links.
_FENCED_CODE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")


def strip_code_spans(content: str) -> str:
    """Remove fenced + inline code spans so example links inside them are not
    extracted as relations. Replaces each span with a space to preserve
    surrounding token boundaries."""
    content = _FENCED_CODE_RE.sub(" ", content)
    content = _INLINE_CODE_RE.sub(" ", content)
    return content


# ── Link extraction from markdown body ───────────────────────

def extract_markdown_links(content: str) -> list[str]:
    """Extract internal document references from markdown links.

    Returns list of targets (paths, doc IDs, or akb:// URIs).
    Filters out external URLs (http/https/mailto) and code-span contents.
    """
    content = strip_code_spans(content)
    targets: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        """Normalize one link target and append it (deduped). Filters
        external URLs / anchors; keeps akb:// URIs verbatim; strips a
        leading './' and any '#fragment' from relative paths."""
        target = raw.strip()
        if not target or target.startswith(("http://", "https://", "mailto:", "#")):
            return
        if target.startswith("akb://"):
            if target not in seen:
                targets.append(target)
                seen.add(target)
            return
        target = target.lstrip("./")
        if "#" in target:
            target = target.split("#")[0]
        if target and target not in seen:
            targets.append(target)
            seen.add(target)

    # [text](target)
    for match in _MD_LINK_RE.finditer(content):
        _add(match.group(2))

    # [[target]] / [[target|alias]] — the alias after '|' is display text,
    # not part of the link, so keep only the target. Without this the
    # alias leaked into the target (see _AKB_URI_RE note above).
    for match in _WIKILINK_RE.finditer(content):
        _add(match.group(1).split("|", 1)[0])

    # Also find bare akb:// URIs in body (not inside a markdown / wiki link)
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

    # Delete only IMPLICIT edges — frontmatter+body links are the source
    # of truth for those, but explicit (akb_link) edges must survive a
    # rewrite. Without the kind filter every akb_update destroys them.
    await conn.execute(
        "DELETE FROM edges WHERE source_uri = $1 AND kind = 'implicit'",
        source,
    )

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

def canonicalize_resource_uri(parsed) -> str | None:
    """Rebuild a doc/table/file URI in canonical 0.3.0 form from its parsed
    parts. Returns None for non-linkable kinds (coll/vault).

    Callers that accept a URI string from outside (akb_link, edge
    extraction) MUST canonicalize before persisting, otherwise a
    legacy-shaped but parseable URI (e.g. the pre-0.3.0
    `akb://V/doc/{coll}/{name}` form) gets stored verbatim and later
    trips migration 026's rewrite against its canonical twin."""
    ident = parsed.identifier or ""
    if parsed.kind == "doc":
        return doc_uri(parsed.vault, ident)
    if parsed.kind == "table":
        return table_uri(parsed.vault, ident, parsed.coll_path)
    if parsed.kind == "file":
        return file_uri(parsed.vault, ident, parsed.coll_path)
    return None


async def link_resources(
    vault_name: str,
    source_uri: str,
    target_uri: str,
    relation_type: str,
    created_by: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Create an explicit edge between any two resources.

    Only doc / table / file are linkable. ``coll`` and vault URIs are
    rejected — collections are navigation aids, not edge endpoints.
    The ``edges.target_type`` CHECK constraint enforces the same
    invariant at the DB layer; this surface-level reject gives the
    caller a clear error instead of a Postgres failure.
    """
    source_parsed = parse_uri(source_uri)
    target_parsed = parse_uri(target_uri)
    if not source_parsed:
        return err(f"Invalid source URI: {source_uri}", code=INVALID_URI)
    if not target_parsed:
        return err(f"Invalid target URI: {target_uri}", code=INVALID_URI)
    _LINKABLE = ("doc", "table", "file")
    if source_parsed.kind not in _LINKABLE:
        return err(
            f"Cannot link from a {source_parsed.kind} URI ({source_uri}). "
            f"Linkable kinds: {_LINKABLE}.",
            code=INVALID_ARGUMENT,
        )
    if target_parsed.kind not in _LINKABLE:
        return err(
            f"Cannot link to a {target_parsed.kind} URI ({target_uri}). "
            f"Linkable kinds: {_LINKABLE}.",
            code=INVALID_ARGUMENT,
        )

    source_vault_name = source_parsed.vault
    source_type = source_parsed.kind
    source_id = source_parsed.identifier
    target_vault_name = target_parsed.vault
    target_type = target_parsed.kind
    target_id = target_parsed.identifier

    # Linkable kinds (doc/table/file) always carry an identifier; a None
    # here means a malformed URI slipped past parse_uri. Fail closed so the
    # advisory-lock + existence checks below can treat both ids as str.
    if source_id is None or target_id is None:
        return err("Source or target URI is missing an identifier.", code=INVALID_URI)

    # Canonicalize both endpoints from parsed parts so a legacy-shaped
    # URI passed by an external caller is stored in 0.3.0 canonical form
    # (akb://V/coll/<path>/<kind>/<id>) — not verbatim. Verbatim legacy
    # edges are what migration 026 keeps colliding on at boot.
    source_uri = canonicalize_resource_uri(source_parsed) or source_uri
    target_uri = canonicalize_resource_uri(target_parsed) or target_uri

    if source_uri == target_uri:
        return err("Cannot link a resource to itself", code=SELF_LINK)

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # All reads + INSERT inside one TX so a concurrent delete on
            # either endpoint cannot leave a dangling edge.
            vault = await conn.fetchrow(
                "SELECT id FROM vaults WHERE name = $1", vault_name,
            )
            if not vault:
                return err(f"Vault not found: {vault_name}", code=NOT_FOUND)
            vault_id = vault["id"]

            source_vault = await conn.fetchrow(
                "SELECT id FROM vaults WHERE name = $1", source_vault_name,
            )
            if not source_vault:
                return err(f"Source vault not found: {source_vault_name}", code=NOT_FOUND)
            target_vault = await conn.fetchrow(
                "SELECT id FROM vaults WHERE name = $1", target_vault_name,
            )
            if not target_vault:
                return err(f"Target vault not found: {target_vault_name}", code=NOT_FOUND)

            # Acquire the same path advisory lock the doc write paths use so
            # akb_link serialises with akb_delete / akb_update on either
            # endpoint. Doc endpoints only — table/file lifecycle is keyed by
            # UUID and doesn't go through path_lock. Sort by (vault_id, id) to
            # impose a deadlock-free lock-acquisition order.
            from app.repositories.document_repo import acquire_path_lock
            doc_endpoints: list[tuple[uuid.UUID, str]] = []
            if source_type == "doc":
                doc_endpoints.append((source_vault["id"], source_id))
            if target_type == "doc":
                doc_endpoints.append((target_vault["id"], target_id))
            for vid, ident in sorted(
                doc_endpoints, key=lambda x: (str(x[0]), x[1]),
            ):
                await acquire_path_lock(conn, vid, ident)

            if not await _resource_exists(
                conn, source_vault["id"], source_type, source_id,
            ):
                return err(f"Source resource not found: {source_uri}", code=NOT_FOUND)
            if not await _resource_exists(
                conn, target_vault["id"], target_type, target_id,
            ):
                return err(f"Target resource not found: {target_uri}", code=NOT_FOUND)

            await conn.execute(
                """
                INSERT INTO edges (id, vault_id, source_uri, target_uri, relation_type,
                                   source_type, target_type, metadata, created_by, kind)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'explicit')
                ON CONFLICT (source_uri, target_uri, relation_type) DO UPDATE
                SET metadata = $8, created_by = $9, kind = 'explicit'
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
    *,
    vault_id: uuid.UUID | None = None,
) -> dict:
    """Remove an edge between two resources.

    ``vault_id`` scopes the DELETE so a future caller can't accidentally
    delete an edge from another vault by spelling its URIs.
    The MCP handler already gates by vault access, but the service-level
    interface now enforces it too.

    Endpoints are validated and canonicalized first, exactly as
    ``link_resources`` does on write, so the two surfaces accept and
    reject identically. A non-canonical but parseable input (slash-
    suffixed or legacy ``akb://V/doc/{coll}/{name}`` shape) is rewritten
    to the canonical form link stored — otherwise the DELETE would
    silently remove nothing. An unparseable URI, or a non-linkable
    ``coll``/``vault`` URI (which ``link_resources`` rejects up front),
    is rejected here too rather than falling through to a zero-row DELETE
    that returns a misleading ``{"unlinked": 0}`` success — that false
    success is the asymmetry the matching ``POST`` 400 would never show.
    Both callers (MCP ``akb_unlink`` and REST ``DELETE /relations``)
    inherit this; resolution stays centralized in the service, not
    duplicated per surface.
    """
    # Mirror link_resources' validation + canonicalization. canonicalize_
    # resource_uri returns None for exactly the non-linkable kinds
    # (coll/vault), so a None result is the same "not a linkable kind"
    # signal link_resources rejects with INVALID_ARGUMENT.
    src_parsed = parse_uri(source_uri)
    tgt_parsed = parse_uri(target_uri)
    if not src_parsed:
        return err(f"Invalid source URI: {source_uri}", code=INVALID_URI)
    if not tgt_parsed:
        return err(f"Invalid target URI: {target_uri}", code=INVALID_URI)
    src_canon = canonicalize_resource_uri(src_parsed)
    tgt_canon = canonicalize_resource_uri(tgt_parsed)
    if src_canon is None:
        return err(
            f"Cannot unlink from a {src_parsed.kind} URI ({source_uri}). "
            "Linkable kinds: ('doc', 'table', 'file').",
            code=INVALID_ARGUMENT,
        )
    if tgt_canon is None:
        return err(
            f"Cannot unlink to a {tgt_parsed.kind} URI ({target_uri}). "
            "Linkable kinds: ('doc', 'table', 'file').",
            code=INVALID_ARGUMENT,
        )
    source_uri = src_canon
    target_uri = tgt_canon

    pool = await get_pool()
    async with pool.acquire() as conn:
        params: list = [source_uri, target_uri]
        where = "source_uri = $1 AND target_uri = $2"
        if relation_type:
            where += " AND relation_type = $3"
            params.append(relation_type)
        if vault_id is not None:
            where += f" AND vault_id = ${len(params) + 1}"
            params.append(vault_id)
        result = await conn.execute(
            f"DELETE FROM edges WHERE {where}", *params,
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
    hops: int = 2,
    limit: int = 50,
    *,
    vault_id: uuid.UUID | None = None,
) -> dict:
    """Get a subgraph centered on a resource, or the full vault graph.

    Returns { nodes: [...], edges: [...] } suitable for visualization.
    Nodes include all resource types (doc, table, file).

    ``hops`` is the BFS traversal radius in edge hops — distinct from
    ``akb_browse.depth`` which counts collection-tree levels. 0.3.0
    renamed the parameter to make the difference visible at every
    call site.

    `vault_id` scopes the graph to one vault: both the BFS edge fetch
    and the name resolution refuse to look outside it. Caller must
    have verified reader access on this vault. When `vault_id` is
    omitted we look it up by name as a convenience for legacy
    callers — but the auth check is still the caller's responsibility.

    With no ``resource_uri`` this delegates to :func:`get_overview` so the
    whole-vault graph has ONE code path: degree-ranked, deterministic, and
    carrying honest totals (``nodes_total``/``truncated``/…). That replaced an
    earlier inline branch which capped on ``created_at DESC`` — an arbitrary
    recency slice whose composition drifted with ``limit``. ``limit`` becomes
    the overview's node ``top_k`` (a node cap is a cleaner visualization
    safety net than an edge cap).
    """
    if not resource_uri:
        return await get_overview(vault, vault_id=vault_id, top_k=limit)

    pool = await get_pool()
    nodes: dict[str, dict] = {}
    edge_list: list[dict] = []

    async with pool.acquire() as conn:
        if vault_id is None:
            vault_row = await conn.fetchrow("SELECT id FROM vaults WHERE name = $1", vault)
            if not vault_row:
                return {"nodes": [], "edges": []}
            vault_id = vault_row["id"]

        await _bfs_collect(conn, vault_id, vault, resource_uri, hops, limit, nodes, edge_list)

    return {
        "nodes": list(nodes.values()),
        "edges": edge_list,
    }


async def _fetch_orphan_nodes(
    conn, vault: str, vault_id, *,
    doc_paths: set[str], table_names: set[str], file_ids: set[str], limit: int,
) -> tuple[list[dict], bool]:
    """Resources — non-archived docs, tables, files — that appear in NO edge,
    returned as degree-0 isolated nodes. Without these, a vault whose resources
    aren't linked yet (e.g. a tables-only vault) renders a BLANK canvas: the
    overview is built from edge endpoints, so an unlinked resource is never an
    endpoint and would otherwise be invisible everywhere in the graph.

    The "is it connected?" test is pushed into SQL as an anti-join on the
    resource's OWN IDENTITY column — doc ``path`` / table ``name`` / file ``id``,
    NOT the full URI. Two reasons:
      - The per-catalog ``LIMIT`` then caps the number of ORPHANS, not "recent
        rows" — so a vault whose newest N rows are all linked still surfaces its
        older orphans (a recency-windowed Python filter would miss them).
      - Identity (not URI) matching is robust to a non-canonical collection
        segment on a stored edge URI: a table is uniquely the vault's ``name``,
        a file its ``id`` — so a link written with a mis-cased ``/coll/…/`` can't
        make a linked table/file re-appear as a phantom orphan duplicate.

    The three types are interleaved (round-robin) before the cap so a flood of
    orphan docs never starves orphan tables/files (the motivating case).
    """
    per = max(0, limit)
    doc_rows = await conn.fetch(
        "SELECT path, title FROM documents "
        "WHERE vault_id = $1 AND status != 'archived' AND path != ALL($2::text[]) "
        "ORDER BY created_at DESC LIMIT $3",
        vault_id, list(doc_paths), per,
    )
    tbl_rows = await conn.fetch(
        "SELECT t.name, t.description, c.path AS coll "
        "FROM vault_tables t LEFT JOIN collections c ON t.collection_id = c.id "
        "WHERE t.vault_id = $1 AND t.name != ALL($2::text[]) "
        "ORDER BY t.created_at DESC LIMIT $3",
        vault_id, list(table_names), per,
    )
    file_rows = await conn.fetch(
        "SELECT f.id::text AS id, f.name, c.path AS coll "
        "FROM vault_files f LEFT JOIN collections c ON f.collection_id = c.id "
        "WHERE f.vault_id = $1 AND f.id::text != ALL($2::text[]) "
        "ORDER BY f.created_at DESC LIMIT $3",
        vault_id, list(file_ids), per,
    )

    docs = [{"uri": doc_uri(vault, r["path"]), "resource_type": "doc",
             "name": r["title"] or doc_uri(vault, r["path"]), "degree": 0} for r in doc_rows]
    tables = [{"uri": table_uri(vault, r["name"], r["coll"]), "resource_type": "table",
               "name": r["description"] or r["name"], "degree": 0} for r in tbl_rows]
    files = [{"uri": file_uri(vault, r["id"], r["coll"]), "resource_type": "file",
              "name": r["name"] or file_uri(vault, r["id"], r["coll"]), "degree": 0} for r in file_rows]

    # Round-robin interleave, then cap at `limit` so each type is represented.
    merged = [n for trio in zip_longest(docs, tables, files) for n in trio if n is not None]
    # Honest truncation signal (parallel to the connected `truncated`): any
    # catalog that hit its LIMIT may have more orphans we didn't fetch, or the
    # interleaved set exceeded the cap. Lets the client say "+N more unlinked"
    # instead of silently implying these are ALL the orphans.
    truncated = (
        len(doc_rows) >= per or len(tbl_rows) >= per or len(file_rows) >= per
        or len(merged) > limit
    )
    return merged[:limit], truncated


async def get_overview(
    vault: str,
    *,
    vault_id: uuid.UUID | None = None,
    top_k: int = 200,
    include_orphans: bool = True,
    orphan_limit: int = 500,
) -> dict:
    """Whole-vault overview: the `top_k` highest-DEGREE nodes plus the edges
    induced among them, with honest totals so the client can render an explicit
    "showing N of M" instead of silently truncating. With `include_orphans`,
    unlinked resources (docs/tables/files in no edge) are appended as degree-0
    isolated nodes so a vault with no relations yet still renders its resources
    instead of a blank canvas; the client's "Hide orphans" toggle governs them.

    Response contract: ``nodes_total`` / ``returned`` / ``truncated`` describe
    the CONNECTED graph only (the degree-ranked slice), while the ``nodes`` array
    additionally carries the orphan nodes. So ``len(nodes) == returned +
    orphans_returned`` — do NOT assume ``len(nodes) <= nodes_total``. The
    "showing N of M" banner reads ``returned``/``nodes_total`` (connected); the
    orphans are extra and toggle-hideable, and ``orphans_truncated`` says whether
    the orphan set itself was capped at ``orphan_limit`` (more unlinked resources
    exist than returned).

    Supersedes the old no-`resource_uri` branch of :func:`get_graph`, which
    capped on ``created_at DESC`` (an arbitrary recency slice whose composition
    drifted with `limit`). Degree ranking is deterministic and surfaces the
    structurally important hubs — the nodes a reader actually wants to see first
    in an overview — regardless of when they were created.

    Caller must have verified reader access; ``vault_id`` scopes every query.
    When omitted it is resolved by name for legacy callers (auth still theirs).
    """
    empty = {
        "nodes": [], "edges": [],
        "nodes_total": 0, "edges_total": 0, "returned": 0, "truncated": False,
        "orphans_returned": 0, "orphans_truncated": False,
    }
    pool = await get_pool()
    async with pool.acquire() as conn:
        if vault_id is None:
            vault_row = await conn.fetchrow("SELECT id FROM vaults WHERE name = $1", vault)
            if not vault_row:
                return empty
            vault_id = vault_row["id"]

        totals = await conn.fetchrow(
            """
            WITH endpoints AS (
                SELECT source_uri AS uri FROM edges WHERE vault_id = $1
                UNION ALL
                SELECT target_uri FROM edges WHERE vault_id = $1
            )
            SELECT (SELECT count(*) FROM edges WHERE vault_id = $1) AS edges_total,
                   (SELECT count(DISTINCT uri) FROM endpoints) AS nodes_total
            """,
            vault_id,
        )
        nodes_total = totals["nodes_total"] if totals else 0
        edges_total = totals["edges_total"] if totals else 0
        # NB: do NOT early-return when nodes_total == 0 — a vault may have
        # unlinked resources (orphans) to render even with zero edges. The
        # degree query below simply yields nothing in that case.

        # Top-`top_k` endpoints by degree. `degree` here is INCIDENT-EDGE count
        # (count(*) over the endpoint stream) — the same definition the canvas
        # uses (use-graph-data.ts `degreeMap`), so node sizing stays consistent
        # between server ranking and client render. Parallel edges (the same
        # pair joined by several relation_types) therefore each add 1, which is
        # intended: more relationships = more central. A node's resource_type is
        # consistent wherever it appears (a doc URI is always 'doc'), so
        # max(rtype) just picks that single value. The `uri` tie-break makes the
        # cut deterministic when several nodes share the boundary degree.
        top_rows = await conn.fetch(
            """
            WITH endpoints AS (
                SELECT source_uri AS uri, source_type AS rtype FROM edges WHERE vault_id = $1
                UNION ALL
                SELECT target_uri, target_type FROM edges WHERE vault_id = $1
            )
            SELECT uri, max(rtype) AS rtype, count(*) AS degree
            FROM endpoints
            GROUP BY uri
            ORDER BY degree DESC, uri
            LIMIT $2
            """,
            vault_id, top_k,
        )

        nodes: dict[str, dict] = {}
        for r in top_rows:
            nodes[r["uri"]] = {
                "uri": r["uri"],
                "resource_type": r["rtype"],
                "name": r["uri"],
                "degree": r["degree"],
            }
        top_uris = list(nodes.keys())

        edge_list: list[dict] = []
        if top_uris:
            # Induced subgraph: only edges whose BOTH endpoints are in the
            # top-K set — so the overview never paints a dangling stub to a
            # node it didn't include.
            edge_rows = await conn.fetch(
                """
                SELECT source_uri, target_uri, relation_type, kind
                FROM edges
                WHERE vault_id = $1
                  AND source_uri = ANY($2::text[])
                  AND target_uri = ANY($2::text[])
                """,
                vault_id, top_uris,
            )
            for r in edge_rows:
                edge_list.append({
                    "source": r["source_uri"],
                    "target": r["target_uri"],
                    "relation": r["relation_type"],
                    "kind": r["kind"],
                })

        names = await _batch_resolve_names(
            conn, [(u, n["resource_type"]) for u, n in nodes.items()], vault_id=vault_id
        )
        for uri, node in nodes.items():
            node["name"] = names.get(uri) or uri

        connected_count = len(nodes)

        # Append unlinked resources as degree-0 isolated nodes (so a tables-only
        # / freshly-seeded vault renders its resources, not a blank canvas).
        # `_fetch_orphan_nodes` anti-joins each catalog against the set of
        # connected IDENTITIES (doc paths / table names / file ids), so a
        # linked-but-low-degree resource is never mistaken for an orphan — and
        # identity (not URI) matching is robust to a non-canonical collection
        # segment on a stored edge URI. Identities come from EVERY edge endpoint
        # (not just the top-K shown); UNION ALL since the Python sets dedup.
        orphans_truncated = False
        if include_orphans and orphan_limit > 0:
            doc_paths: set[str] = set()
            table_names: set[str] = set()
            file_ids: set[str] = set()
            if nodes_total:
                conn_rows = await conn.fetch(
                    "SELECT source_uri AS uri FROM edges WHERE vault_id = $1 "
                    "UNION ALL SELECT target_uri FROM edges WHERE vault_id = $1",
                    vault_id,
                )
                for r in conn_rows:
                    p = parse_uri(r["uri"])
                    if not p or not p.identifier:
                        # An edge endpoint we can't parse drops out of the
                        # connected-identity set, so its resource could surface
                        # as a phantom orphan — log it (matches the other
                        # edge-skip debug lines in this module) rather than fail
                        # silently. Validated edges don't hit this; legacy/
                        # malformed URIs might.
                        logger.debug(
                            "overview orphan-partition: unparseable edge endpoint "
                            "%r in vault %s", r["uri"], vault,
                        )
                        continue
                    if p.kind == "doc":
                        doc_paths.add(p.identifier)
                    elif p.kind == "table":
                        table_names.add(p.identifier)
                    elif p.kind == "file":
                        file_ids.add(p.identifier)
            orphan_nodes, orphans_truncated = await _fetch_orphan_nodes(
                conn, vault, vault_id,
                doc_paths=doc_paths, table_names=table_names, file_ids=file_ids,
                limit=orphan_limit,
            )
            for o in orphan_nodes:
                if o["uri"] not in nodes:
                    nodes[o["uri"]] = o

    return {
        "nodes": list(nodes.values()),
        "edges": edge_list,
        "nodes_total": nodes_total,
        "edges_total": edges_total,
        "returned": connected_count,
        "truncated": connected_count < nodes_total,
        "orphans_returned": len(nodes) - connected_count,
        "orphans_truncated": orphans_truncated,
    }


async def get_health(
    vault: str,
    *,
    vault_id: uuid.UUID | None = None,
    hub_threshold: int = 5,
    limit: int = 20,
) -> dict:
    """KB-health audit for one vault: the over-connected HUBS (degree ≥
    ``hub_threshold``) and the ORPHAN documents (no relation at all — the
    undiscoverable corners a graph view exists to surface).

    "Orphan" means *no relation within THIS vault's graph*. Vault isolation
    scopes the connected set to ``edges WHERE vault_id = vault`` — the same
    edges the vault's graph view renders — so a doc referenced only from another
    vault (that incoming edge lives in the OTHER vault's graph and never shows
    here) is correctly an orphan *of this graph*. ``hubs`` degree is incident-
    edge count, matching :func:`get_overview` and the canvas.

    Orphans are computed over documents only (the dominant resource type in the
    graph); tables/files are intentionally out of v1 scope. This is an on-demand
    *audit* endpoint (not a hot path), so it scans every non-archived doc once to
    count orphans exactly; a SQL anti-join would bound the cost for very large
    vaults and is the natural next step if this becomes a dashboard auto-refresh.
    Reader-scoped via ``vault_id``; ``vault`` (the name) builds the doc URIs
    matched against the connected set.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if vault_id is None:
            vault_row = await conn.fetchrow("SELECT id FROM vaults WHERE name = $1", vault)
            if not vault_row:
                return {"hubs": [], "orphans": {"count": 0, "sample": []}}
            vault_id = vault_row["id"]

        hub_rows = await conn.fetch(
            """
            WITH endpoints AS (
                SELECT source_uri AS uri, source_type AS rtype FROM edges WHERE vault_id = $1
                UNION ALL
                SELECT target_uri, target_type FROM edges WHERE vault_id = $1
            )
            SELECT uri, max(rtype) AS rtype, count(*) AS degree
            FROM endpoints
            GROUP BY uri
            HAVING count(*) >= $2
            ORDER BY degree DESC, uri
            LIMIT $3
            """,
            vault_id, hub_threshold, limit,
        )

        # Every URI that appears on either end of an edge → "connected".
        connected_rows = await conn.fetch(
            """
            SELECT source_uri AS uri FROM edges WHERE vault_id = $1
            UNION
            SELECT target_uri FROM edges WHERE vault_id = $1
            """,
            vault_id,
        )
        connected = {r["uri"] for r in connected_rows}

        # Orphans: non-archived docs whose URI is in no edge.
        doc_rows = await conn.fetch(
            "SELECT path, title FROM documents WHERE vault_id = $1 AND status != 'archived'",
            vault_id,
        )
        orphan_count = 0
        orphan_sample: list[dict] = []
        for r in doc_rows:
            uri = doc_uri(vault, r["path"])
            if uri in connected:
                continue
            orphan_count += 1
            if len(orphan_sample) < limit:
                orphan_sample.append({
                    "uri": uri,
                    "name": r["title"] or uri,
                    "resource_type": "doc",
                })

        hubs: list[dict] = []
        if hub_rows:
            hub_names = await _batch_resolve_names(
                conn, [(r["uri"], r["rtype"]) for r in hub_rows], vault_id=vault_id
            )
            hubs = [
                {
                    "uri": r["uri"],
                    "name": hub_names.get(r["uri"]) or r["uri"],
                    "resource_type": r["rtype"],
                    "degree": r["degree"],
                }
                for r in hub_rows
            ]

    return {
        "hubs": hubs,
        "orphans": {"count": orphan_count, "sample": orphan_sample},
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
            WHERE d.id::text = $1 AND d.vault_id = $2
            """,
            doc_id, vault_id,
        )
        if not row:
            return err("Document not found", code=NOT_FOUND)

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
        identifier = parsed.identifier
        if identifier is None:
            continue
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
            doc_uri_opt = doc_uris.get(r["path"])
            if doc_uri_opt:
                names[doc_uri_opt] = r["title"]
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
            tbl_uri_opt = table_uris.get(r["name"])
            if tbl_uri_opt:
                names[tbl_uri_opt] = r["description"] if r["description"] else r["name"]
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
            file_uri_opt = file_uris.get(r["id"])
            if file_uri_opt:
                names[file_uri_opt] = r["name"]
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
    """Resolve target reference and insert edge. Returns True if stored.

    Only doc / table / file are linkable resources. ``coll`` and the
    vault-only form ``akb://{vault}`` are navigation handles — semantically
    a "link to a collection" doesn't have a clear meaning (is it a link
    to every doc inside? to the folder concept itself?), and the
    ``edges.target_type`` CHECK constraint enforces the same invariant
    at the DB layer. This filter is what keeps body-text mentions of
    coll URIs (e.g. "see ``akb://V/coll/X`` for the spec") from
    silently failing at INSERT.
    """
    # If target is already an akb:// URI, parse it directly
    parsed = parse_uri(target_ref)
    if parsed:
        if parsed.kind not in ("doc", "table", "file"):
            # `coll` / `vault` URIs are navigation aids, not link
            # targets — skip silently. Logged at DEBUG so an
            # operator running noisy edge-extraction can still see
            # how many got filtered.
            logger.debug(
                "Skipping edge target %r: %s URIs are not linkable",
                target_ref, parsed.kind,
            )
            return False
        target_type = parsed.kind
        ident = parsed.identifier or ""
        # Resolve the target's vault (edges may cross vaults) so existence
        # is checked against the right catalog.
        target_vault_id = await conn.fetchval(
            "SELECT id FROM vaults WHERE name = $1", parsed.vault,
        )
        if target_vault_id is None:
            logger.debug(
                "Skipping edge to %r: target vault %r not found",
                target_ref, parsed.vault,
            )
            return False
        # Validate the target EXISTS before storing — via the SAME
        # `_resource_exists` primitive the explicit akb_link path uses, so
        # the two link paths validate identically (they differ only in
        # policy: akb_link returns NOT_FOUND, extraction silently skips).
        # An implicit edge to a non-existent resource can never be drawn and
        # only pollutes the graph — e.g. a wikilink whose alias leaked into
        # the path (`…/x.md|Label`), or a forward reference to a doc never
        # created. The extraction path used to skip this check, which is how
        # the malformed targets got persisted.
        if not await _resource_exists(conn, target_vault_id, parsed.kind, ident):
            logger.debug(
                "Skipping edge to nonexistent %s %r", parsed.kind, target_ref,
            )
            return False
        # Rebuild from parsed parts so surface variants collapse under the
        # edges uniqueness convention — otherwise ON CONFLICT can't dedupe.
        if parsed.kind == "doc":
            target_uri = doc_uri(parsed.vault, ident)
        elif parsed.kind == "table":
            target_uri = table_uri(parsed.vault, ident, parsed.coll_path)
        else:
            target_uri = file_uri(parsed.vault, ident, parsed.coll_path)
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
    """Resolve a non-URI document reference to its PG UUID.

    Used by `_store_edge` for the *legacy* path where a doc's frontmatter
    `depends_on` / `related_to` list contains a bare string instead of
    an `akb://` URI. New code is URI-first and bypasses this function
    entirely (the URI path in `_store_edge` short-circuits before us).

    Match arms (in order, each by exact / suffix / UUID — never by
    substring; the legacy substring arm was a wrong-doc magnet after
    the URI cutover):
      1. UUID — `id = $2`
      2. Exact path — `path = $2`
      3. Trailing-segment match — `path LIKE '%/' || $2` (e.g. ref
         `api.md` matches `notes/api.md` iff there is exactly one such
         doc; the unique constraint on `(vault_id, path)` does NOT
         dedupe across collections, so this can still return one of
         several matches — but the suffix is anchored at `/`, so
         `api.md` cannot match `funapi.md`).
    """
    # UUID + exact-path arms share the same predicate as `find_by_ref`
    # / `drill_down` — keep the substring-match ban centralised.
    from app.repositories.document_repo import DocumentRepository
    row = await conn.fetchrow(
        f"SELECT id FROM documents d WHERE vault_id = $1 AND {DocumentRepository.match_clause(2)}",
        vault_id, ref,
    )
    if row:
        return row["id"]

    # Trailing-segment match: `api.md` ↔ `notes/api.md`. Anchored at
    # `/` so it can't match arbitrary substrings.
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
    # Track emitted edges so an edge A→B doesn't appear twice when both
    # A and B are processed in the same BFS wave.
    emitted: set[tuple[str, str, str]] = set()

    for current_depth in range(depth + 1):
        if not queue or len(nodes) >= limit:
            break

        next_queue: list[str] = []

        unvisited = [u for u in queue if u not in visited]
        if not unvisited:
            break

        outgoing = await conn.fetch(
            "SELECT source_uri, target_uri, target_type, relation_type, kind "
            "FROM edges WHERE source_uri = ANY($1::text[]) AND vault_id = $2",
            unvisited, vault_id,
        )
        incoming = await conn.fetch(
            "SELECT source_uri, source_type, target_uri, relation_type, kind "
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
            rtype = parsed.kind

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
                key = (uri, r["target_uri"], r["relation_type"])
                if key not in emitted:
                    emitted.add(key)
                    edges.append({
                        "source": uri,
                        "target": r["target_uri"],
                        "relation": r["relation_type"],
                        "kind": r["kind"],
                    })
                if r["target_uri"] not in visited:
                    next_queue.append(r["target_uri"])

            for r in in_by_uri.get(uri, []):
                key = (r["source_uri"], uri, r["relation_type"])
                if key not in emitted:
                    emitted.add(key)
                    edges.append({
                        "source": r["source_uri"],
                        "target": uri,
                        "relation": r["relation_type"],
                        "kind": r["kind"],
                    })
                if r["source_uri"] not in visited:
                    next_queue.append(r["source_uri"])

        queue = next_queue

    # The node cap (`len(nodes) >= limit`) can truncate the final BFS wave
    # after an edge to one of its targets was already emitted — leaving a
    # dangling edge to a node that was never materialized. Drop those so the
    # client never receives a stub endpoint it has no node for.
    edges[:] = [e for e in edges if e["source"] in nodes and e["target"] in nodes]

    uris_to_resolve = [(uri, n["resource_type"]) for uri, n in nodes.items()]
    names = await _batch_resolve_names(conn, uris_to_resolve, vault_id=vault_id)
    for uri, node in nodes.items():
        node["name"] = names.get(uri) or uri


# ── URI builder helpers for callers ──────────────────────────

