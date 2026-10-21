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

import asyncio
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from itertools import zip_longest
from typing import Literal, NamedTuple, cast, get_args

from markdown_it import MarkdownIt

from app.exceptions import ValidationError
from app.db.postgres import get_pool
from app.repositories.vault_files_repo import confirmed_file_predicate
from app.services.asset_service import ASSET_URL_PREFIX
from app.services.edge_boundary import (
    LINKABLE_RESOURCE_TYPES,
    edge_scope_sql,
    uri_is_in_vault,
    vault_uri_prefix,
)
from app.services.uri_service import parse_uri, doc_uri, table_uri, file_uri
from app.util.errors import (
    err,
    INVALID_ARGUMENT,
    INVALID_URI,
    NOT_FOUND,
    SELF_LINK,
)
from app.util.text import like_escape

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

# ── Edge strength — single source of truth ───────────────────
#
# How strongly two resources are connected is its own attribute. It is neither
# the relation name (`related_to`, `derived_from`, … say what a link MEANS and
# are too ambiguous to imply strength) nor the provenance in `edges.kind`
# (`implicit` = computed from a document's frontmatter / body, `explicit` =
# made with `akb_link`).
#
#   - an explicit edge is `strong` unless its creator said `weak`;
#   - an implicit edge is ALWAYS `weak`. It is derived, so the value is
#     computed when an edge is served and never stored.
#
# Only explicit edges keep a value, under the `edge_type` key of
# `edges.metadata` — no column, no migration. A row without the key, which is
# every `akb_link` edge made before this attribute existed, therefore reads as
# `strong` with no backfill.
#
# `resolve_edge_type` is the one place that turns stored state into the served
# value. The REST models, the OpenAPI contract and the MCP `akb_link` schema
# derive their vocabulary from `EdgeType`, like `LinkRelationType` above.
EdgeType = Literal["strong", "weak"]
EDGE_TYPES: tuple[str, ...] = get_args(EdgeType)
DEFAULT_EDGE_TYPE: EdgeType = "strong"  # what an explicit edge reads as unless said otherwise
IMPLICIT_EDGE_TYPE: EdgeType = "weak"  # what an implicit edge is, always
EDGE_TYPE_METADATA_KEY = "edge_type"
# Selected next to `kind` by every query that serves an edge. The raw value is
# only an input to `resolve_edge_type`; it is never served as it is.
_STORED_EDGE_TYPE_COLUMN = f"metadata->>'{EDGE_TYPE_METADATA_KEY}' AS stored_edge_type"


def resolve_edge_type(kind: str | None, stored: str | None) -> EdgeType:
    """The ``edge_type`` served for an edge row.

    ``kind`` is ``edges.kind`` and ``stored`` is ``edges.metadata->>'edge_type'``
    (NULL when the key is absent). Anything but an explicit edge is weak
    whatever its metadata says, so a stray key on an implicit row can never
    promote it. An explicit edge reads its stored value; a missing or
    unrecognised one reads as the default, so no row is served outside the
    vocabulary.
    """
    if kind != "explicit":
        return IMPLICIT_EDGE_TYPE
    if stored in EDGE_TYPES:
        return cast(EdgeType, stored)
    return DEFAULT_EDGE_TYPE


def edge_strength_sql(edge_type: str | None, *, alias: str = "") -> str:
    """SQL predicate for the edges whose served ``edge_type`` is ``edge_type``.

    The SQL twin of :func:`resolve_edge_type`, for a query that has to *choose*
    edges by strength rather than label the ones it fetched — the strength-aware
    neighbourhood walk. It is the same rule written once more, so a test pins
    the two together over every kind / stored-value shape:

    * weak   — an implicit edge, or an explicit one that stored ``weak``;
    * strong — an explicit edge that did not store ``weak`` (no key, a
      non-object ``metadata``, an unrecognised value: all the cases
      :func:`resolve_edge_type` serves as the default).

    ``None`` means "no restriction" and yields an empty string; any other value
    outside the vocabulary raises, because the fragment is interpolated into
    SQL and must only ever be built from the constants above.
    """
    if edge_type is None:
        return ""
    if edge_type not in EDGE_TYPES:
        raise ValueError(f"edge_type must be one of {list(EDGE_TYPES)}, got {edge_type!r}")
    col = f"{alias}." if alias else ""
    stored = f"{col}metadata->>'{EDGE_TYPE_METADATA_KEY}'"
    if edge_type == "weak":
        return f"({col}kind <> 'explicit' OR {stored} = 'weak')"
    return f"({col}kind = 'explicit' AND {stored} IS DISTINCT FROM 'weak')"


# ── Neighbourhood walk direction — single source of truth ────
#
# The BFS behind `GET /graph?uri=…` and `akb_graph(uri=…)` can take the edges of
# a node it has reached in two ways:
#
#   - `both` (the default): every edge touching the node, whichever way it
#     points. Through a hub — a cluster forty incidents point at — the walk fans
#     out to everything that points at the hub.
#   - `outward`: the node's own context, which is its OUTGOING edges (the
#     cluster, node, service and owner it names), plus two kinds of INCOMING
#     edge, which say what hangs off the node rather than what it names:
#     `derived_from` (what was derived from it: a postmortem derived_from the
#     incident) and `depends_on` (what depends on it, and so is affected by
#     it: a service that depends_on the failed one, then the services that
#     depend on that one, a dependency level per hop). Any other incoming
#     edge (`related_to`, `references`, `links_to`, …) is some other
#     resource's context, so the walk stops there instead of fanning out
#     through it. A node many others depend on is therefore still walked
#     through: those dependents are what the relation is followed for.
#
# The vocabulary and the relations `outward` still follows against the arrow
# are defined here; the REST route, the MCP `akb_graph` schema and the tests
# derive from them, like `EdgeType` above.
GraphDirection = Literal["both", "outward"]
GRAPH_DIRECTIONS: tuple[str, ...] = get_args(GraphDirection)
DEFAULT_GRAPH_DIRECTION: GraphDirection = "both"
# A tuple, not a set: the SQL fragment below is built from it, and its text must
# not depend on hash order. Every member is a relation `akb_link` can write.
OUTWARD_INCOMING_RELATIONS: tuple[LinkRelationType, ...] = ("derived_from", "depends_on")


def incoming_walk_sql(direction: str | None) -> str:
    """SQL predicate narrowing the walk's *incoming* edge fetch for ``direction``.

    ``both`` (and ``None``, an omitted parameter) adds nothing: the walk takes
    every incoming edge. ``outward`` keeps the incoming edges whose relation is
    one of :data:`OUTWARD_INCOMING_RELATIONS` and no others. Only the incoming
    fetch is ever narrowed — an outgoing edge is followed in every direction
    mode.

    Like :func:`edge_strength_sql`, the fragment is interpolated into SQL, so
    it is built only from the constants above (a constant ``IN`` list, so the
    fetch binds the same arguments in every mode) and any value outside the
    vocabulary raises.
    """
    if direction is None or direction == "both":
        return ""
    if direction not in GRAPH_DIRECTIONS:
        raise ValueError(f"direction must be one of {list(GRAPH_DIRECTIONS)}, got {direction!r}")
    relations = ", ".join(f"'{relation}'" for relation in OUTWARD_INCOMING_RELATIONS)
    return f"relation_type IN ({relations})"


def validate_new_structured_relation_refs(
    vault: str,
    refs: list[str],
    *,
    existing_refs: list[str] | None = None,
) -> None:
    """Reject newly introduced structured references outside ``vault``.

    Existing values are accepted so an unrelated edit does not make a legacy
    document unwritable. They are inert in the graph because extraction skips
    cross-vault targets. New values fail before Git or PostgreSQL mutation and
    before any target-vault catalog lookup, so target existence is not exposed.
    """
    existing = set(existing_refs or [])
    for ref in refs:
        if ref in existing:
            continue
        parsed = parse_uri(ref)
        if parsed is not None and parsed.vault != vault:
            raise ValidationError(
                "Structured relations must stay within the document's vault; "
                "use an ordinary Markdown link for a cross-vault reference."
            )

# CommonMark parser handles escaped labels, reference-style links, angle-bracket
# destinations, and optional link titles. Relative images can represent
# document-to-document references in imported Markdown; generated editor asset
# URLs are filtered below because their lifecycle is not a graph edge.
_MARKDOWN_PARSER = MarkdownIt("commonmark")
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

def normalize_document_link_ref(ref: str) -> str:
    """Normalize Markdown's path prefixes to an AKB vault-relative ref.

    Historical AKB documents use vault-relative paths even when authors spell
    them with ``./``, one or more ``../`` segments, or a leading ``/``.  Remove
    only those complete prefixes.  ``str.lstrip('./')`` is intentionally not
    used: it treats its argument as a character set and corrupts legitimate
    names such as ``.well-known.md``.
    """
    normalized = ref
    while True:
        if normalized.startswith("../"):
            normalized = normalized[3:]
        elif normalized.startswith("./"):
            normalized = normalized[2:]
        elif normalized.startswith("/"):
            normalized = normalized[1:]
        else:
            return normalized


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
        external URLs / anchors; keeps akb:// URIs verbatim; normalizes
        explicit vault-relative prefixes and strips any '#fragment'."""
        target = raw.strip()
        if not target or target.startswith(("http://", "https://", "mailto:", "//", "#")):
            return
        if target.lower().startswith(ASSET_URL_PREFIX):
            return
        if target.startswith("akb://"):
            if target not in seen:
                targets.append(target)
                seen.add(target)
            return
        if "#" in target:
            target = target.split("#")[0]
        target = normalize_document_link_ref(target)
        if target and target not in seen:
            targets.append(target)
            seen.add(target)

    # Standard inline/reference links and images. Use the parser rather than a
    # parenthesis regex so a canonical target remains intact when the author
    # adds a Markdown title or uses an angle-bracket destination.
    pending = list(reversed(_MARKDOWN_PARSER.parse(content)))
    while pending:
        token = pending.pop()
        if token.children:
            pending.extend(reversed(token.children))
        if token.type == "link_open":
            href = token.attrGet("href")
            _add(href if isinstance(href, str) else "")
        elif token.type == "image":
            source = token.attrGet("src")
            _add(source if isinstance(source, str) else "")

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
    source_resource_id: uuid.UUID | None = None,
) -> int:
    """Store all edges from a document: frontmatter fields + markdown body links.

    ``source_resource_id`` is the native ledger's identity for the writing
    document, when the native arm owns it. Passing it scopes the implicit
    clear below to THIS resource instead of to whatever currently answers at
    ``doc_path`` — a moved resource's rewrite would otherwise erase the
    implicit edges of the resource that has since taken the freed path.

    Returns total number of edges stored.
    """
    source = doc_uri(vault_name, doc_path)

    # Delete only IMPLICIT edges — frontmatter+body links are the source
    # of truth for those, but explicit (akb_link) edges must survive a
    # rewrite. Without the kind filter every akb_update destroys them.
    #
    # Scoped by identity when we have one: that also covers the rows this
    # resource still owns under a PREVIOUS path, so a move needs no separate
    # old-path sweep. The URI arm stays for rows that predate identity
    # stamping, and is restricted to rows no resource has claimed.
    if source_resource_id is not None:
        await conn.execute(
            """
            DELETE FROM edges
             WHERE vault_id = $1 AND kind = 'implicit'
               AND (source_resource_id = $2
                    OR (source_resource_id IS NULL AND source_uri = $3))
            """,
            vault_id, source_resource_id, source,
        )
    else:
        await conn.execute(
            "DELETE FROM edges WHERE source_uri = $1 AND kind = 'implicit'",
            source,
        )

    count = 0

    for target_ref in depends_on:
        if await _store_edge(conn, vault_id, vault_name, source, "doc", target_ref, "depends_on", source_resource_id):
            count += 1

    for target_ref in related_to:
        if await _store_edge(conn, vault_id, vault_name, source, "doc", target_ref, "related_to", source_resource_id):
            count += 1

    for target_ref in implements:
        if await _store_edge(conn, vault_id, vault_name, source, "doc", target_ref, "implements", source_resource_id):
            count += 1

    # Body markdown links
    body_links = extract_markdown_links(body_content)
    for target_ref in body_links:
        if await _store_edge(conn, vault_id, vault_name, source, "doc", target_ref, "links_to", source_resource_id):
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


async def _relink_edge_column(conn, vault_id, column: str, old_uri: str, new_uri: str) -> None:
    """Repoint edges referencing ``old_uri`` to ``new_uri`` on ``column``
    ('source_uri' or 'target_uri') during a move. Rows that would duplicate an
    edge already present at ``new_uri`` are skipped by the UPDATE (the
    UNIQUE(source_uri, target_uri, relation_type) guard) and then dropped,
    since they ARE such duplicates. ``column`` comes from a fixed internal set,
    so the f-string interpolation is not an injection surface."""
    other = "target_uri" if column == "source_uri" else "source_uri"
    await conn.execute(
        f"""
        UPDATE edges e SET {column} = $1
         WHERE e.vault_id = $2 AND e.{column} = $3
           AND NOT EXISTS (
             SELECT 1 FROM edges x
              WHERE x.vault_id = $2 AND x.{column} = $1
                AND x.{other} = e.{other}
                AND x.relation_type = e.relation_type)
        """,
        new_uri, vault_id, old_uri,
    )
    await conn.execute(
        f"DELETE FROM edges WHERE vault_id = $1 AND {column} = $2",
        vault_id, old_uri,
    )


async def relink_resource_edges(conn, vault_id, old_uri: str, new_uri: str) -> None:
    """Carry a moved resource's edges from its old URI to its new one.

    Both endpoints are rewritten: the resource's own outgoing edges and every
    edge that pointed at it. Without this a move silently drops the explicit
    links an agent made with `akb_link` — the old URI keeps resolving through
    the move alias, but the graph rows still name a path nothing writes to.

    One helper for both revision arms. The legacy move had this inline; the
    native move needs the identical behaviour, and two copies of a rewrite
    whose correctness rests on a UNIQUE guard is how they drift.
    """
    await _relink_edge_column(conn, vault_id, "source_uri", old_uri, new_uri)
    await _relink_edge_column(conn, vault_id, "target_uri", old_uri, new_uri)


# ── Identity-anchored maintenance (native arm) ────────────────
#
# The helpers above find a resource's edges by the URI its path spells. That
# holds on the bare-Git arm, where the `documents` row and the path it owns are
# deleted in one transaction. It does not hold on `postgres_native`: identity is
# `native_resources.resource_id`, `current_path` is mutable, a freed path can be
# taken over by a DIFFERENT resource, and the graph work runs after the
# authoritative commit — so "the edges at this URI" and "this resource's edges"
# are two different sets exactly when it matters (akb#654, akb#655).
#
# These find rows by identity instead, and write the resource's CURRENT path
# rather than replaying an old→new transition. That is what makes them safe to
# run late, twice, or out of order.


async def _repoint_edge_column(
    conn, vault_id, column: str, id_column: str,
    resource_id: uuid.UUID, new_uri: str,
) -> None:
    """Set ``column`` to ``new_uri`` on every edge owned by ``resource_id``.

    Same duplicate handling as :func:`_relink_edge_column`: a row that would
    collide with an edge already present at ``new_uri`` is skipped by the
    UNIQUE guard and then dropped, because it IS that duplicate. ``column`` and
    ``id_column`` come from a fixed internal set, so the f-string interpolation
    is not an injection surface.
    """
    other = "target_uri" if column == "source_uri" else "source_uri"
    await conn.execute(
        f"""
        UPDATE edges e SET {column} = $1
         WHERE e.vault_id = $2 AND e.{id_column} = $3 AND e.{column} <> $1
           AND NOT EXISTS (
             SELECT 1 FROM edges x
              WHERE x.vault_id = $2 AND x.{column} = $1
                AND x.{other} = e.{other}
                AND x.relation_type = e.relation_type)
        """,
        new_uri, vault_id, resource_id,
    )
    await conn.execute(
        f"DELETE FROM edges WHERE vault_id = $1 AND {id_column} = $2 AND {column} <> $3",
        vault_id, resource_id, new_uri,
    )


async def adopt_native_document_edges(
    conn, vault_id, vault_name: str, resource_id: uuid.UUID, path: str,
) -> None:
    """Claim the unstamped edges at ``path`` for ``resource_id``.

    Every edge written since identity stamping carries its endpoint's id, but
    rows older than migration 112 — and rows whose path the backfill could not
    attribute — carry none, and identity-keyed maintenance would walk straight
    past them. A move knows one path this resource certainly used to hold, so
    it can adopt what is sitting there.

    Refused when anyone else could own those rows: a live native resource at
    that path means they are ITS rows (that is the akb#654 shape), and a legacy
    catalog row there means the bare-Git arm is the owner. Adoption is a repair
    for rows nobody claims, never a transfer.
    """
    live = await conn.fetchval(
        "SELECT 1 FROM native_resources WHERE namespace_id = $1 "
        "AND surface = 'document' AND lifecycle = 'live' AND current_path = $2",
        vault_id, path,
    )
    if live:
        return
    legacy = await conn.fetchval(_DOC_EXISTS_LEGACY, vault_id, path)
    if legacy:
        return
    uri = doc_uri(vault_name, path)
    await conn.execute(
        "UPDATE edges SET source_resource_id = $1 WHERE vault_id = $2 "
        "AND source_uri = $3 AND source_type = 'doc' AND source_resource_id IS NULL",
        resource_id, vault_id, uri,
    )
    await conn.execute(
        "UPDATE edges SET target_resource_id = $1 WHERE vault_id = $2 "
        "AND target_uri = $3 AND target_type = 'doc' AND target_resource_id IS NULL",
        resource_id, vault_id, uri,
    )


async def sync_native_document_edge_uris(
    conn, vault_id, vault_name: str, resource_id: uuid.UUID, path: str,
    *, adopt_path: str | None = None,
) -> None:
    """Point every edge this native document owns at its current ``path``.

    Convergent, not transitional. `relink_resource_edges` replays one
    old→new step, so two moves whose post-commit hooks finish in the opposite
    order leave the edge on the intermediate path: the delayed A→B hook
    rewrites an endpoint the B→C hook had already carried to C (akb#655).
    The transition is not replayed here — the caller reads the resource's head
    path under its row lock and passes that, so a late or repeated run writes
    the same value the timely one did.

    ``adopt_path`` is a path this resource is known to have held; unclaimed
    rows there are adopted first, so an edge older than identity stamping still
    moves. See :func:`adopt_native_document_edges` for when that is refused.
    """
    if adopt_path is not None and adopt_path != path:
        await adopt_native_document_edges(
            conn, vault_id, vault_name, resource_id, adopt_path,
        )
    new_uri = doc_uri(vault_name, path)
    await _repoint_edge_column(
        conn, vault_id, "source_uri", "source_resource_id", resource_id, new_uri,
    )
    await _repoint_edge_column(
        conn, vault_id, "target_uri", "target_resource_id", resource_id, new_uri,
    )


async def delete_native_document_edges(
    conn, vault_id, vault_name: str, resource_id: uuid.UUID, path: str,
) -> None:
    """Remove every edge a deleted native document was an endpoint of.

    Two passes, and both are needed:

    1. By identity — the rows this resource owns, whatever path they name.
       This is the whole set for anything written since identity stamping,
       and it is why a delayed delete no longer reaches a replacement
       document's links (akb#654).
    2. By URI, but only for rows carrying no identity AND only while NOBODY
       owns the path. Those are the rows that predate stamping; leaving them
       behind when the path is genuinely gone would be the opposite defect —
       a deleted document keeping its edges. "Nobody" means neither a live
       native replacement (the akb#654 shape) nor a legacy catalog row: a
       cutover leaves pre-cutover documents in `documents`, and
       `_resolve_document_endpoint` answers with the legacy row first, so an
       edge at that URI is the legacy document's. The unconditional
       `delete_document_relations` this replaces erased those too.
    """
    await conn.execute(
        """
        DELETE FROM edges
         WHERE vault_id = $1
           AND (source_resource_id = $2 OR target_resource_id = $2)
        """,
        vault_id, resource_id,
    )
    replacement = await conn.fetchval(
        "SELECT 1 FROM native_resources WHERE namespace_id = $1 "
        "AND surface = 'document' AND lifecycle = 'live' AND current_path = $2",
        vault_id, path,
    )
    if replacement:
        return
    if await conn.fetchval(_DOC_EXISTS_LEGACY, vault_id, path):
        return
    uri = doc_uri(vault_name, path)
    await conn.execute(
        """
        DELETE FROM edges
         WHERE vault_id = $1
           AND ((source_uri = $2 AND source_resource_id IS NULL)
                OR (target_uri = $2 AND target_resource_id IS NULL))
        """,
        vault_id, uri,
    )


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


# The write behind `link_resources`: one atomic upsert, so two links of the same
# edge cannot interleave a read of its strength with a write of it.
#
# `$12` is the strength the caller named, or NULL when it named none. NULL is not
# "strong"; it is "said nothing about it", and what that means depends on the row:
#
#   - a new edge is strong;
#   - an existing explicit edge keeps the strength it holds (a value outside the
#     vocabulary, a missing key or a non-object `metadata` reads as strong, as
#     `resolve_edge_type` serves it, and is written out as strong);
#   - an existing implicit edge has no stored strength to keep: it is derived,
#     always weak, and whatever its `metadata` says means nothing. Linking over it
#     makes a new explicit edge, which is strong.
#
# A strength that was named always wins, up or down. The reserved key is written
# last, so neither the caller's `metadata` (`$8`) nor the stored object can
# override it. `RETURNING` hands back the strength now stored, which is what the
# link answers with. Every fragment spliced in below is a module constant.
_EDGE_TYPE_VOCABULARY_SQL = ", ".join(f"'{value}'" for value in EDGE_TYPES)
_LINK_UPSERT_SQL = f"""
    INSERT INTO edges (id, vault_id, source_uri, target_uri, relation_type,
                       source_type, target_type, metadata, created_by, kind,
                       source_resource_id, target_resource_id)
    VALUES ($1, $2, $3, $4, $5, $6, $7,
            $8::jsonb || jsonb_build_object(
                '{EDGE_TYPE_METADATA_KEY}', COALESCE($12::text, '{DEFAULT_EDGE_TYPE}')),
            $9, 'explicit', $10, $11)
    ON CONFLICT (source_uri, target_uri, relation_type) DO UPDATE
    SET metadata = (CASE WHEN jsonb_typeof(edges.metadata) = 'object'
                         THEN edges.metadata ELSE '{{}}'::jsonb END)
                   || $8::jsonb
                   || jsonb_build_object('{EDGE_TYPE_METADATA_KEY}', COALESCE($12::text,
                          CASE WHEN edges.kind = 'explicit'
                                AND edges.metadata->>'{EDGE_TYPE_METADATA_KEY}'
                                    IN ({_EDGE_TYPE_VOCABULARY_SQL})
                               THEN edges.metadata->>'{EDGE_TYPE_METADATA_KEY}'
                               ELSE '{DEFAULT_EDGE_TYPE}' END)),
        created_by = $9, kind = 'explicit',
        source_resource_id = EXCLUDED.source_resource_id,
        target_resource_id = EXCLUDED.target_resource_id
    RETURNING {_STORED_EDGE_TYPE_COLUMN}
"""


async def link_resources(
    vault_name: str,
    source_uri: str,
    target_uri: str,
    relation_type: str,
    created_by: str | None = None,
    metadata: dict | None = None,
    edge_type: str | None = None,
) -> dict:
    """Create an explicit edge between any two resources.

    Only doc / table / file are linkable. ``coll`` and vault URIs are
    rejected — collections are navigation aids, not edge endpoints.
    The ``edges.target_type`` CHECK constraint enforces the same
    invariant at the DB layer; this surface-level reject gives the
    caller a clear error instead of a Postgres failure.

    ``edge_type`` ("strong" or "weak") is the connection's strength. It is
    stored in ``edges.metadata`` under the reserved ``edge_type`` key, merged
    with the caller's ``metadata`` so neither replaces the other; a same-named
    key inside ``metadata`` is ignored in favour of this argument.

    ``None`` (the default) names no strength. A new edge is then strong, and
    linking an edge that already exists leaves its strength as it is: a caller
    that predates the attribute, or one that only updates ``metadata``, cannot
    promote a weak edge by leaving the field out. A value that is given sets the
    strength, up or down. The result's ``edge_type`` is the strength now stored.
    """
    if edge_type is not None and edge_type not in EDGE_TYPES:
        return err(
            f"edge_type must be one of {list(EDGE_TYPES)}, got {edge_type!r}",
            code=INVALID_ARGUMENT,
        )

    source_parsed = parse_uri(source_uri)
    target_parsed = parse_uri(target_uri)
    if not source_parsed:
        return err(f"Invalid source URI: {source_uri}", code=INVALID_URI)
    if not target_parsed:
        return err(f"Invalid target URI: {target_uri}", code=INVALID_URI)
    if source_parsed.kind not in LINKABLE_RESOURCE_TYPES:
        return err(
            f"Cannot link from a {source_parsed.kind} URI ({source_uri}). "
            f"Linkable kinds: {LINKABLE_RESOURCE_TYPES}.",
            code=INVALID_ARGUMENT,
        )
    if target_parsed.kind not in LINKABLE_RESOURCE_TYPES:
        return err(
            f"Cannot link to a {target_parsed.kind} URI ({target_uri}). "
            f"Linkable kinds: {LINKABLE_RESOURCE_TYPES}.",
            code=INVALID_ARGUMENT,
        )

    # Enforce the boundary before any vault or resource lookup. The requested
    # graph owner, source authority, and target authority must all agree; this
    # also prevents a direct service caller from assigning a valid edge to an
    # unrelated vault container.
    if not source_parsed.vault == target_parsed.vault == vault_name:
        return err(
            "source and target must belong to the requested vault",
            code=INVALID_ARGUMENT,
        )

    source_type = source_parsed.kind
    source_id = source_parsed.identifier
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

            # Acquire the same path advisory lock the doc write paths use so
            # akb_link serialises with akb_delete / akb_update on either
            # endpoint. Doc endpoints only — table/file lifecycle is keyed by
            # UUID and doesn't go through path_lock. Sort by (vault_id, id) to
            # impose a deadlock-free lock-acquisition order.
            from app.repositories.document_repo import acquire_path_lock
            doc_endpoints: list[tuple[uuid.UUID, str]] = []
            if source_type == "doc":
                doc_endpoints.append((vault_id, source_id))
            if target_type == "doc":
                doc_endpoints.append((vault_id, target_id))
            for vid, ident in sorted(
                doc_endpoints, key=lambda x: (str(x[0]), x[1]),
            ):
                await acquire_path_lock(conn, vid, ident)

            # Doc endpoints resolve rather than merely exist: the resolution
            # carries the native ledger's identity, and an explicit link is
            # precisely the edge that must survive the target being moved.
            source_resource_id: uuid.UUID | None = None
            target_resource_id: uuid.UUID | None = None
            if source_type == "doc":
                endpoint = await _resolve_document_endpoint(conn, vault_id, source_id)
                if endpoint is None:
                    return err(f"Source resource not found: {source_uri}", code=NOT_FOUND)
                source_resource_id = endpoint.resource_id
            elif not await _resource_exists(conn, vault_id, source_type, source_id):
                return err(f"Source resource not found: {source_uri}", code=NOT_FOUND)
            if target_type == "doc":
                endpoint = await _resolve_document_endpoint(conn, vault_id, target_id)
                if endpoint is None:
                    return err(f"Target resource not found: {target_uri}", code=NOT_FOUND)
                target_resource_id = endpoint.resource_id
            elif not await _resource_exists(conn, vault_id, target_type, target_id):
                return err(f"Target resource not found: {target_uri}", code=NOT_FOUND)

            # The strength travels beside the caller's `metadata`, not inside
            # it: the statement merges the stored object with this call's keys
            # (this call winning a collision), so changing `edge_type` keeps the
            # caller's other keys and the caller's keys cannot drop or set the
            # strength. The reserved key is dropped from what the caller sent;
            # the statement would override it anyway.
            caller_metadata = {
                key: value for key, value in (metadata or {}).items()
                if key != EDGE_TYPE_METADATA_KEY
            }
            written = await conn.fetchrow(
                _LINK_UPSERT_SQL,
                uuid.uuid4(), vault_id, source_uri, target_uri, relation_type,
                source_type, target_type, json.dumps(caller_metadata), created_by,
                source_resource_id, target_resource_id, edge_type,
            )

    # What the edge holds now, which is the strength asked for or, when none was
    # asked for, the one it already had (strong for a new edge).
    stored_edge_type = resolve_edge_type("explicit", written["stored_edge_type"])
    logger.info("Linked %s → %s (%s, %s)", source_uri, target_uri, relation_type, stored_edge_type)
    return {
        "linked": True,
        "source": source_uri,
        "target": target_uri,
        "relation": relation_type,
        "edge_type": stored_edge_type,
    }


async def unlink_resources(
    source_uri: str,
    target_uri: str,
    relation_type: str | None = None,
    *,
    vault_id: uuid.UUID,
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
    if src_parsed.vault != tgt_parsed.vault:
        return err(
            "source and target must belong to the same vault",
            code=INVALID_ARGUMENT,
        )
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
        owner_vault = await conn.fetchval(
            "SELECT name FROM vaults WHERE id = $1", vault_id,
        )
        if owner_vault is None:
            return err("Vault not found", code=NOT_FOUND)
        if owner_vault != src_parsed.vault:
            return err(
                "source and target must belong to the authorized vault",
                code=INVALID_ARGUMENT,
            )
        params: list = [source_uri, target_uri]
        # Implicit edges are owned by document metadata/body extraction. They
        # must be removed by editing that source content, not by the explicit
        # relation API (which would only make them reappear on the next save).
        where = "source_uri = $1 AND target_uri = $2 AND kind = 'explicit'"
        if relation_type:
            where += " AND relation_type = $3"
            params.append(relation_type)
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

    ``vault_id`` and both endpoint URI authorities must identify ``vault``.
    Legacy cross-vault rows are omitted completely: neither their raw URI nor
    their contribution to a count is part of the reader-visible graph.

    Every row carries ``kind`` (provenance: implicit | explicit) and
    ``edge_type`` (strength: strong | weak — see :func:`resolve_edge_type`).

    A row also names the resource at its other end: ``name`` is its title when
    it has one, and a document additionally carries ``title`` / ``doc_type`` /
    ``tags`` / ``path`` (see :func:`_batch_resolve_labels`, one batch for the
    whole result — never a lookup per row). Any resolved resource also carries
    ``updated_at``, when it last changed (UTC); a row whose resource has no time
    to give states none.
    """
    if not uri_is_in_vault(resource_uri, vault):
        return []

    pool = await get_pool()
    results = []
    uris_to_resolve: list[tuple[str, str]] = []
    prefix = vault_uri_prefix(vault)
    scoped = edge_scope_sql(alias="e", vault_param=2, prefix_param=3)

    async with pool.acquire() as conn:
        if direction in ("outgoing", "both"):
            base_params: list = [resource_uri, vault_id, prefix]
            if relation_type:
                rows = await conn.fetch(
                    f"""
                    SELECT e.relation_type, e.target_uri, e.target_type, e.kind,
                           e.{_STORED_EDGE_TYPE_COLUMN}
                    FROM edges e
                    WHERE e.source_uri = $1 AND {scoped} AND e.relation_type = $4
                    """,
                    *base_params, relation_type,
                )
            else:
                rows = await conn.fetch(
                    f"""
                    SELECT e.relation_type, e.target_uri, e.target_type, e.kind,
                           e.{_STORED_EDGE_TYPE_COLUMN}
                    FROM edges e
                    WHERE e.source_uri = $1 AND {scoped}
                    """,
                    *base_params,
                )
            for r in rows:
                if not uri_is_in_vault(r["target_uri"], vault):
                    continue
                results.append({
                    "direction": "outgoing",
                    "relation": r["relation_type"],
                    "uri": r["target_uri"],
                    "resource_type": r["target_type"],
                    "kind": r["kind"],
                    "edge_type": resolve_edge_type(r["kind"], r["stored_edge_type"]),
                })
                uris_to_resolve.append((r["target_uri"], r["target_type"]))

        if direction in ("incoming", "both"):
            base_params = [resource_uri, vault_id, prefix]
            if relation_type:
                rows = await conn.fetch(
                    f"""
                    SELECT e.relation_type, e.source_uri, e.source_type, e.kind,
                           e.{_STORED_EDGE_TYPE_COLUMN}
                    FROM edges e
                    WHERE e.target_uri = $1 AND {scoped} AND e.relation_type = $4
                    """,
                    *base_params, relation_type,
                )
            else:
                rows = await conn.fetch(
                    f"""
                    SELECT e.relation_type, e.source_uri, e.source_type, e.kind,
                           e.{_STORED_EDGE_TYPE_COLUMN}
                    FROM edges e
                    WHERE e.target_uri = $1 AND {scoped}
                    """,
                    *base_params,
                )
            for r in rows:
                if not uri_is_in_vault(r["source_uri"], vault):
                    continue
                results.append({
                    "direction": "incoming",
                    "relation": r["relation_type"],
                    "uri": r["source_uri"],
                    "resource_type": r["source_type"],
                    "kind": r["kind"],
                    "edge_type": resolve_edge_type(r["kind"], r["stored_edge_type"]),
                })
                uris_to_resolve.append((r["source_uri"], r["source_type"]))

        labels = await _batch_resolve_labels(
            conn, uris_to_resolve, vault_id=vault_id, vault_name=vault,
        )
        for entry in results:
            entry.update(labels.get(entry["uri"], {}))

    return results


async def get_graph(
    vault: str,
    resource_uri: str | None = None,
    hops: int = 2,
    limit: int = 50,
    *,
    vault_id: uuid.UUID | None = None,
    edge_type: str | None = None,
    direction: str | None = None,
) -> dict:
    """Get a subgraph centered on a resource, or the full vault graph.

    Returns { nodes: [...], edges: [...] } suitable for visualization.
    Nodes include all resource types (doc, table, file). Every edge carries
    ``kind`` (implicit | explicit) and ``edge_type`` (strong | weak). Every
    node is named by its title when it has one (``name``); a document also
    carries ``title`` / ``doc_type`` / ``tags`` / ``path`` — see
    :func:`_batch_resolve_labels`. A node also carries ``updated_at``, when its
    resource last changed (UTC), and states none when that is unknown.

    ``hops`` is the BFS traversal radius in edge hops — distinct from
    ``akb_browse.depth`` which counts collection-tree levels. 0.3.0
    renamed the parameter to make the difference visible at every
    call site.

    ``edge_type`` ("strong" or "weak"; ``None`` = every edge, the default)
    restricts the walk to edges of that strength, in whichever directions
    ``direction`` follows. The restriction is part of the *traversal*, not a
    filter on its result: a neighbour reachable only through an excluded edge
    is never visited, so the ``limit`` node cap is spent on the neighbours the
    caller asked for. It belongs to the neighbourhood walk, so it needs
    ``resource_uri``; the whole-vault overview is not strength-aware and a
    request that pairs it with ``edge_type`` is refused instead of silently
    returning every edge. A neighbourhood result also says whether ``limit``
    cut it short (``truncated``).

    ``direction`` ("both", the default and what ``None`` means, or "outward")
    chooses which edges of a reached node the walk follows. ``both`` takes
    every edge touching it; ``outward`` takes its outgoing edges plus what was
    derived from it and what depends on it (its incoming ``derived_from`` and
    ``depends_on`` edges), so the walk ends at shared context instead of
    fanning out through a hub. It composes with ``edge_type`` (each narrows
    what the other leaves) and, like it, belongs to the neighbourhood walk:
    ``outward`` without ``resource_uri`` is refused. ``both`` is what the
    overview already does, so it is accepted there as the no-op it is.

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
    if edge_type is not None:
        # Checked before anything else, so a bad value never reaches SQL and a
        # request the overview cannot honour is refused rather than ignored.
        if edge_type not in EDGE_TYPES:
            return err(
                f"edge_type must be one of {list(EDGE_TYPES)}, got {edge_type!r}",
                code=INVALID_ARGUMENT,
            )
        if not resource_uri:
            return err(
                "edge_type restricts the neighbourhood walk, so it needs `uri`; "
                "the vault overview always returns every edge",
                code=INVALID_ARGUMENT,
                hint="Pass `uri` for a strength-aware subgraph, or drop `edge_type` for the vault overview.",
            )
    if direction is not None:
        # Same order as `edge_type`: the value is checked before the pairing.
        if direction not in GRAPH_DIRECTIONS:
            return err(
                f"direction must be one of {list(GRAPH_DIRECTIONS)}, got {direction!r}",
                code=INVALID_ARGUMENT,
            )
        if direction != DEFAULT_GRAPH_DIRECTION and not resource_uri:
            return err(
                f"direction={direction} restricts the neighbourhood walk, so it needs `uri`; "
                "the vault overview always returns every edge",
                code=INVALID_ARGUMENT,
                hint="Pass `uri` for a directional subgraph, or drop `direction` for the vault overview.",
            )
    if not resource_uri:
        return await get_overview(vault, vault_id=vault_id, top_k=limit)
    if not uri_is_in_vault(resource_uri, vault):
        return {"nodes": [], "edges": [], "truncated": False}

    pool = await get_pool()
    nodes: dict[str, dict] = {}
    edge_list: list[dict] = []

    async with pool.acquire() as conn:
        if vault_id is None:
            vault_row = await conn.fetchrow("SELECT id FROM vaults WHERE name = $1", vault)
            if not vault_row:
                return {"nodes": [], "edges": [], "truncated": False}
            vault_id = vault_row["id"]

        truncated = await _bfs_collect(
            conn, vault_id, vault, resource_uri, hops, limit, nodes, edge_list,
            edge_type=edge_type, direction=direction,
        )

    return {
        "nodes": list(nodes.values()),
        "edges": edge_list,
        "truncated": truncated,
    }


# The overview's orphan documents are enumerated from the `documents` catalog on
# either authority (see the CHANGELOG), so their time is the catalog row's — except
# on a native installation, where a row that survived cutover is no longer written
# by document edits: the resource that shares its id holds the newer, true time.
# Nothing native is read on a legacy installation.
_ORPHAN_DOCS_LEGACY_SQL = (
    "SELECT path, title, doc_type, tags, updated_at FROM documents "
    "WHERE vault_id = $1 AND status != 'archived' AND path != ALL($2::text[]) "
    "ORDER BY created_at DESC LIMIT $3"
)
_ORPHAN_DOCS_NATIVE_SQL = (
    "SELECT d.path, d.title, d.doc_type, d.tags, "
    "COALESCE(r.updated_at, d.updated_at) AS updated_at "
    "FROM documents d LEFT JOIN native_resources r "
    "ON r.resource_id = d.id AND r.namespace_id = d.vault_id "
    "AND r.surface = 'document' AND r.lifecycle = 'live' "
    "WHERE d.vault_id = $1 AND d.status != 'archived' AND d.path != ALL($2::text[]) "
    "ORDER BY d.created_at DESC LIMIT $3"
)


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
    from app.services.document_counters import native_documents_are_authoritative

    doc_rows = await conn.fetch(
        _ORPHAN_DOCS_NATIVE_SQL if native_documents_are_authoritative() else _ORPHAN_DOCS_LEGACY_SQL,
        vault_id, list(doc_paths), per,
    )
    tbl_rows = await conn.fetch(
        "SELECT t.name, t.description, t.updated_at, c.path AS coll "
        "FROM vault_tables t LEFT JOIN collections c ON t.collection_id = c.id "
        "WHERE t.vault_id = $1 AND t.name != ALL($2::text[]) "
        "ORDER BY t.created_at DESC LIMIT $3",
        vault_id, list(table_names), per,
    )
    file_rows = await conn.fetch(
        "SELECT f.id::text AS id, f.name, f.updated_at, c.path AS coll "
        "FROM vault_files f LEFT JOIN collections c ON f.collection_id = c.id "
        "WHERE f.vault_id = $1 AND " + confirmed_file_predicate("f") + " "
        "AND f.id::text != ALL($2::text[]) "
        "ORDER BY f.created_at DESC LIMIT $3",
        vault_id, list(file_ids), per,
    )

    # Labelled exactly like a connected node (`_batch_resolve_labels`), from the
    # rows already in hand — an orphan costs no extra query.
    docs = [{"uri": doc_uri(vault, r["path"]), "resource_type": "doc", "degree": 0,
             **_doc_label(
                 r["path"], title=r["title"], doc_type=r["doc_type"], tags=r["tags"],
                 updated_at=r.get("updated_at"),
             )}
            for r in doc_rows]
    tables = [{"uri": table_uri(vault, r["name"], r["coll"]), "resource_type": "table", "degree": 0,
               **_table_label(r["name"], r["description"], r.get("updated_at"))} for r in tbl_rows]
    files = [{"uri": file_uri(vault, r["id"], r["coll"]), "resource_type": "file", "degree": 0,
              **_file_label(r["id"], r["name"], r.get("updated_at"))} for r in file_rows]

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

        prefix = vault_uri_prefix(vault)
        scoped = edge_scope_sql(vault_param=1, prefix_param=2)

        totals = await conn.fetchrow(
            f"""
            WITH scoped_edges AS (
                SELECT * FROM edges WHERE {scoped}
            ),
            endpoints AS (
                SELECT source_uri AS uri FROM scoped_edges
                UNION ALL
                SELECT target_uri FROM scoped_edges
            )
            SELECT (SELECT count(*) FROM scoped_edges) AS edges_total,
                   (SELECT count(DISTINCT uri) FROM endpoints) AS nodes_total
            """,
            vault_id, prefix,
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
            f"""
            WITH scoped_edges AS (
                SELECT * FROM edges WHERE {scoped}
            ),
            endpoints AS (
                SELECT source_uri AS uri, source_type AS rtype FROM scoped_edges
                UNION ALL
                SELECT target_uri, target_type FROM scoped_edges
            )
            SELECT uri, max(rtype) AS rtype, count(*) AS degree
            FROM endpoints
            GROUP BY uri
            ORDER BY degree DESC, uri
            LIMIT $3
            """,
            vault_id, prefix, top_k,
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
                f"""
                SELECT source_uri, target_uri, relation_type, kind,
                       {_STORED_EDGE_TYPE_COLUMN}
                FROM edges
                WHERE {scoped}
                  AND source_uri = ANY($3::text[])
                  AND target_uri = ANY($3::text[])
                """,
                vault_id, prefix, top_uris,
            )
            for r in edge_rows:
                edge_list.append({
                    "source": r["source_uri"],
                    "target": r["target_uri"],
                    "relation": r["relation_type"],
                    "kind": r["kind"],
                    "edge_type": resolve_edge_type(r["kind"], r["stored_edge_type"]),
                })

        labels = await _batch_resolve_labels(
            conn, [(u, n["resource_type"]) for u, n in nodes.items()],
            vault_id=vault_id, vault_name=vault,
        )
        for uri, node in nodes.items():
            node.update(labels.get(uri, {}))

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
                    f"SELECT source_uri AS uri FROM edges WHERE {scoped} "
                    f"UNION ALL SELECT target_uri FROM edges WHERE {scoped}",
                    vault_id, prefix,
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

        prefix = vault_uri_prefix(vault)
        scoped = edge_scope_sql(vault_param=1, prefix_param=2)

        hub_rows = await conn.fetch(
            f"""
            WITH scoped_edges AS (
                SELECT * FROM edges WHERE {scoped}
            ),
            endpoints AS (
                SELECT source_uri AS uri, source_type AS rtype FROM scoped_edges
                UNION ALL
                SELECT target_uri, target_type FROM scoped_edges
            )
            SELECT uri, max(rtype) AS rtype, count(*) AS degree
            FROM endpoints
            GROUP BY uri
            HAVING count(*) >= $3
            ORDER BY degree DESC, uri
            LIMIT $4
            """,
            vault_id, prefix, hub_threshold, limit,
        )

        # Every URI that appears on either end of an edge → "connected".
        connected_rows = await conn.fetch(
            f"""
            SELECT source_uri AS uri FROM edges WHERE {scoped}
            UNION
            SELECT target_uri FROM edges WHERE {scoped}
            """,
            vault_id, prefix,
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
            hub_labels = await _batch_resolve_labels(
                conn, [(r["uri"], r["rtype"]) for r in hub_rows],
                vault_id=vault_id, vault_name=vault,
            )
            hubs = [
                {
                    "uri": r["uri"],
                    "name": r["uri"],
                    "resource_type": r["rtype"],
                    "degree": r["degree"],
                    **hub_labels.get(r["uri"], {}),
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


# ── Batch label resolution ───────────────────────────────────
#
# A graph node and a relation row both name the resource at an endpoint. The
# label is looked up once per call, in a batch of at most one query per
# resource type — never one per node — and carries more than a name:
#
#   document   name   its title when it has one, else its vault path (the
#                     value every caller already got for an unresolved one)
#              title  the real title; absent when the document has none
#              doc_type, tags, path
#   table/file name   unchanged: a table's description (else its name), a
#                     file's name. ``title`` repeats it, so a consumer can read
#                     ``title`` on any resolved resource.
#
# Every resolved resource may also carry ``updated_at``: when it last changed,
# as UTC to the second (``YYYY-MM-DDTHH:MM:SSZ``). It comes out of the same
# batched statement as the rest of the label, from the store that owns the
# resource, and is left out when that store has no time for it:
#
#   document, native authority   `native_resources.updated_at`, the moment the head
#                     revision was published by a create, replace, restore or
#                     move. Every writer sets it together with the head revision's
#                     `occurred_at` (`NativeRevisionRepository.set_head`), so the
#                     two are one value, and it is what the native document API
#                     serves as the document's `updated_at`. A document only the
#                     catalog holds (never copied into the ledger) is dated by its
#                     catalog row instead.
#   document, legacy authority   `documents.updated_at`: set by create, update,
#                     edit and move, and by the metadata worker's backfill.
#   table             `vault_tables.updated_at`: set at creation and by every
#                     schema change (columns, unique keys, indexes). Writing rows
#                     does not touch it; the registry keeps no row-level time.
#   file              `vault_files.updated_at`: set when the upload is confirmed
#                     and by every later replacement of the stored bytes and
#                     every size or integrity-metadata update.
#
# A resource the vault does not hold gets its identifier as ``name`` (and, for a
# document, as ``path``) and nothing the catalog would have told: the label never
# invents a fact, a time included. A URI from another vault gets no label at all.
#
# WHERE a document's title lives depends on the installation's document
# authority (`document_counters.native_documents_are_authoritative`):
#
#   * legacy — the `documents` catalog row (`title`, `doc_type`, `tags`);
#   * native — the head revision's frontmatter. The native ledger deliberately
#     keeps no mutable title projection ("title lives in immutable revision
#     frontmatter": `NativeDocumentService._find_native_title_conflict`), and a
#     document written by it gets no `documents` row (`_resolve_document_endpoint`
#     below: the native arm "writes `native_resources` and never touches" the
#     catalog). A lookup that only asks the catalog therefore misses every such
#     document and falls back to its path — which is what the graph, `akb_graph`
#     and `akb_relations` showed for a native vault.
#     A document migrated at cutover can still lean on its retained catalog row
#     for the fields its preserved frontmatter lacks, exactly as
#     `NativeDocumentService._document_frontmatter` fills them.

# Second read window for a document whose frontmatter does not close inside the
# first one (a hub that lists hundreds of relations). Bounded in size and in
# rows: a label is decoration, and a reader must not be able to turn it into an
# unbounded read by writing wide frontmatter. A document past either bound is
# labelled from the first statement alone (its path or retained catalog title,
# and its time), never left without a label.
_LABEL_RETRY_WINDOW_BYTES = 128 * 1024
_LABEL_RETRY_MAX_ROWS = 50

# One batch for every requested path, native head and retained catalog row
# together. Both catalog joins are index lookups (primary key / the
# `(vault_id, path)` unique key); the second only applies when no native
# resource holds the path, so a document the native ledger owns is never labelled
# from a stale catalog row that happens to share its path.
#
# `updated_at` follows the same rule: the native resource's own time when the
# ledger holds the path, and the catalog row's (`dp`, which joins only when no
# resource does) when it does not. The retained row `dn` never dates a native
# document: it is frozen at cutover, and the resource row is not.
_NATIVE_DOC_LABELS_SQL = """
    SELECT req.path AS path,
           r.resource_id AS resource_id,
           COALESCE(r.updated_at, dp.updated_at) AS updated_at,
           p.byte_size AS body_size,
           substring(p.canonical_bytes FROM 1 FOR $3::int) AS body_slice,
           COALESCE(dn.id, dp.id) AS legacy_id,
           COALESCE(dn.title, dp.title) AS legacy_title,
           COALESCE(dn.doc_type, dp.doc_type) AS legacy_doc_type,
           COALESCE(dn.tags, dp.tags) AS legacy_tags
      FROM unnest($2::text[]) AS req(path)
      LEFT JOIN native_resources r
        ON r.namespace_id = $1
       AND r.surface = 'document'
       AND r.lifecycle = 'live'
       AND r.current_path = req.path
      LEFT JOIN native_revisions nr
        ON nr.resource_id = r.resource_id
       AND nr.revision_id = r.head_revision_id
      LEFT JOIN native_payload_manifests pm
        ON pm.payload_manifest_id = nr.payload_manifest_id
      LEFT JOIN m1_reference_payloads p
        ON p.payload_id = pm.private_locator
      LEFT JOIN documents dn
        ON dn.vault_id = $1
       AND dn.id = r.resource_id
      LEFT JOIN documents dp
        ON dp.vault_id = $1
       AND r.resource_id IS NULL
       AND dp.path = req.path
"""


def _non_blank(value: object) -> str | None:
    """``value`` as text when it says something; None for null, blank or a container."""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = str(value)
    return text if text.strip() else None


def _tag_list(value: object) -> list[str]:
    """Tags as a list of non-blank strings (a lone string is one tag)."""
    if isinstance(value, str):
        items: list | tuple = [value]
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        return []
    return [tag for tag in map(_non_blank, items) if tag is not None]


# What asyncpg decodes a timestamptz of `infinity` / `-infinity` as. Neither names
# a moment, so neither dates anything.
_PG_INFINITIES = (datetime.max, datetime.min)


def _utc_stamp(value: object) -> str | None:
    """A catalog time as ``YYYY-MM-DDTHH:MM:SSZ`` (UTC, to the second).

    None when there is no usable time, so the label states none: absent means
    unknown, never "long ago". Truncated rather than rounded, so a stamp is never
    later than the change it dates. A naive value is read as UTC (a TIMESTAMPTZ
    column never yields one; this only keeps a stray value from being read as
    local time).
    """
    if not isinstance(value, datetime) or value.replace(tzinfo=None) in _PG_INFINITIES:
        return None
    try:
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except (OverflowError, ValueError):
        # An offset can push a value near either end of the range out of it.
        return None


def _doc_label(
    path: str, *, title: object, doc_type: object, tags: object, updated_at: object = None,
) -> dict:
    """The label of a document the vault holds."""
    real_title = _non_blank(title)
    label: dict = {"name": real_title or path}
    if real_title is not None:
        label["title"] = real_title
    label["doc_type"] = _non_blank(doc_type) or "note"
    label["tags"] = _tag_list(tags)
    label["path"] = path
    _stamp_label(label, updated_at)
    return label


def _table_label(name: str, description: str | None, updated_at: object = None) -> dict:
    shown = description or name
    label = {"name": shown, "title": shown}
    _stamp_label(label, updated_at)
    return label


def _file_label(file_id: str, name: str | None, updated_at: object = None) -> dict:
    shown = name or file_id
    label = {"name": shown, "title": shown}
    _stamp_label(label, updated_at)
    return label


def _stamp_label(label: dict, updated_at: object) -> None:
    """Add ``updated_at`` to a label when the catalog row had a time to give."""
    stamp = _utc_stamp(updated_at)
    if stamp is not None:
        label["updated_at"] = stamp


def _label_native_row(row, metadata: dict | None) -> dict:
    """One document's label from the joined native / catalog row.

    ``metadata`` is the parsed frontmatter of the head revision, or None when it
    could not be read. A native resource's own frontmatter wins; the retained
    catalog row fills only a field the frontmatter does not state, mirroring
    `native_document_service._fill_from_legacy`. With no native resource the
    catalog row is the whole answer, and with neither the document is unknown.
    """
    path = row["path"]
    has_catalog_row = row["legacy_id"] is not None
    # The time is the row's own, never the frontmatter's: a body kept byte for
    # byte from Git states whatever it stated, and a revision's body is not the
    # moment it was published.
    updated_at = row.get("updated_at")
    if row["resource_id"] is None:
        if not has_catalog_row:
            return {"name": path, "path": path}
        return _doc_label(
            path,
            title=row["legacy_title"],
            doc_type=row["legacy_doc_type"],
            tags=row["legacy_tags"],
            updated_at=updated_at,
        )
    fm = metadata or {}

    def stated(key: str, catalog_column: str) -> object:
        value = fm.get(key)
        if value is None and has_catalog_row:
            return row[catalog_column]
        return value

    return _doc_label(
        path,
        title=stated("title", "legacy_title"),
        doc_type=stated("type", "legacy_doc_type"),
        tags=stated("tags", "legacy_tags"),
        updated_at=updated_at,
    )


def _label_native_rows(rows, *, final: bool) -> tuple[dict[str, dict], list[str]]:
    """Parse every row's frontmatter window and label its document.

    CPU-bound (YAML), so it runs on a worker thread as one hop for the whole
    batch. Returns ``(labels by path, paths whose frontmatter outgrew the
    window)``; the second list is only filled when a later window exists.

    Every row gets a label, a retry candidate included. Such a document is
    labelled from what the row itself holds (its path, the time of the resource,
    and the retained catalog row if it kept one) and queued for a wider read that
    may replace the label. The wider read is capped in rows, so a candidate past
    the cap is never read again and keeps this label: leaving it out would drop
    the time the statement already fetched.
    """
    from app.services.search_service import _filtered_native_metadata

    labels: dict[str, dict] = {}
    retry: list[str] = []
    for row in rows:
        metadata: dict | None = None
        if row["resource_id"] is not None and row["body_slice"] is not None:
            metadata = _filtered_native_metadata(row)
            # None means the envelope opens but does not close inside the
            # window. Worth a wider read only when the body goes on past it.
            if metadata is None and not final and (row["body_size"] or 0) > len(row["body_slice"]):
                retry.append(row["path"])
        labels[row["path"]] = _label_native_row(row, metadata)
    return labels, retry


async def _native_doc_labels(conn, vault_id: uuid.UUID, paths: list[str]) -> dict[str, dict]:
    """Labels for documents on an installation whose document authority is native.

    One query for the whole batch; a second, bounded one only for the rare
    document whose frontmatter does not fit the first read window. The second
    read replaces a label and never creates one: a document it does not reach
    keeps the label the first read gave it (`_label_native_rows`).
    """
    from app.services.search_service import NATIVE_CANDIDATE_FRONTMATTER_SLICE_BYTES

    # The candidate-filter window, +1 so a closing delimiter that ends exactly on
    # its boundary is still seen — one definition of "the leading envelope".
    windows = (NATIVE_CANDIDATE_FRONTMATTER_SLICE_BYTES + 1, _LABEL_RETRY_WINDOW_BYTES)
    labels: dict[str, dict] = {}
    pending = list(paths)
    for stage, window in enumerate(windows):
        if not pending:
            break
        rows = await conn.fetch(_NATIVE_DOC_LABELS_SQL, vault_id, pending, window)
        found, pending = await asyncio.to_thread(
            _label_native_rows, rows, final=stage == len(windows) - 1,
        )
        labels.update(found)
        pending = pending[:_LABEL_RETRY_MAX_ROWS]
    return labels


async def _batch_resolve_labels(
    conn, uris: list[tuple[str, str]],
    *,
    vault_id: uuid.UUID,
    vault_name: str,
) -> dict[str, dict]:
    """Resolve multiple URIs to display labels in batch (3 queries max, not N).

    Returns ``{uri: label}``; see the block comment above for what a label
    holds and where each field comes from. Every label has a non-empty
    ``name``, so a caller can ``node.update(label)`` without a fallback.

    Scoped to both ``vault_id`` and ``vault_name``. A URI from another vault is
    ignored rather than receiving an identifier fallback; callers must not be
    able to project even the raw foreign endpoint through this helper.
    """
    if not uris:
        return {}

    # identifier -> every URI spelling that names it
    doc_uris: dict[str, list[str]] = {}
    table_uris: dict[str, list[str]] = {}
    file_uris: dict[str, list[str]] = {}

    for uri, rtype in uris:
        parsed = parse_uri(uri)
        if not parsed or parsed.vault != vault_name:
            continue
        identifier = parsed.identifier
        if identifier is None:
            continue
        if rtype == "doc":
            doc_uris.setdefault(identifier, []).append(uri)
        elif rtype == "table":
            table_uris.setdefault(identifier, []).append(uri)
        elif rtype == "file":
            file_uris.setdefault(identifier, []).append(uri)

    labels: dict[str, dict] = {}

    if doc_uris:
        from app.services.document_counters import native_documents_are_authoritative

        found: dict[str, dict]
        if native_documents_are_authoritative():
            found = await _native_doc_labels(conn, vault_id, list(doc_uris))
        else:
            rows = await conn.fetch(
                "SELECT path, title, doc_type, tags, updated_at FROM documents "
                "WHERE path = ANY($1::text[]) AND vault_id = $2",
                list(doc_uris), vault_id,
            )
            found = {
                r["path"]: _doc_label(
                    r["path"], title=r["title"], doc_type=r.get("doc_type"), tags=r.get("tags"),
                    updated_at=r.get("updated_at"),
                )
                for r in rows
            }
        # Unresolved (cross-vault or genuinely missing): fall back to the path
        # so the visualization still renders something.
        for path, spellings in doc_uris.items():
            for uri in spellings:
                labels[uri] = found.get(path) or {"name": path, "path": path}

    if table_uris:
        rows = await conn.fetch(
            "SELECT name, description, updated_at FROM vault_tables "
            "WHERE name = ANY($1::text[]) AND vault_id = $2",
            list(table_uris), vault_id,
        )
        tables = {
            r["name"]: _table_label(r["name"], r["description"], r.get("updated_at")) for r in rows
        }
        for tname, spellings in table_uris.items():
            for uri in spellings:
                labels[uri] = tables.get(tname) or {"name": tname}

    if file_uris:
        rows = await conn.fetch(
            "SELECT id::text, name, updated_at FROM vault_files f "
            "WHERE id::text = ANY($1::text[]) AND vault_id = $2 "
            "AND " + confirmed_file_predicate("f"),
            list(file_uris), vault_id,
        )
        files = {r["id"]: _file_label(r["id"], r["name"], r.get("updated_at")) for r in rows}
        for fid, spellings in file_uris.items():
            for uri in spellings:
                labels[uri] = files.get(fid) or {"name": fid}

    return labels


# ── Helpers ───────────────────────────────────────────────────

_DOC_EXISTS_LEGACY = "SELECT 1 FROM documents WHERE vault_id = $1 AND path = $2"
_NATIVE_DOC_SELECT = (
    "SELECT resource_id, current_path FROM native_resources "
    "WHERE namespace_id = $1 AND surface = 'document' AND lifecycle = 'live' "
)
_NATIVE_DOC_BY_PATH = _NATIVE_DOC_SELECT + "AND current_path = $2"
# Same two arms as `DocumentRepository.match_clause`, so a reference the legacy
# catalog would accept is accepted here too — and the substring-match ban is
# inherited rather than restated.
_NATIVE_DOC_BY_REF = _NATIVE_DOC_SELECT + "AND (resource_id::text = $2 OR current_path = $2)"
_NATIVE_DOC_BY_SUFFIX = (
    _NATIVE_DOC_SELECT + "AND current_path LIKE '%/' || $2 ESCAPE '\\' LIMIT 2"
)


class DocumentEndpoint(NamedTuple):
    """A resolved document endpoint.

    ``path`` is the canonical vault-relative path the edge URI is built from.
    ``resource_id`` is the native ledger's stable identity for it, or None when
    the endpoint is a legacy `documents` row — the legacy arm needs no anchor,
    because there the catalog row and the path it owns are deleted in the same
    transaction.
    """

    path: str
    resource_id: uuid.UUID | None


async def _resolve_document_endpoint(
    conn, vault_id: uuid.UUID, path: str,
) -> DocumentEndpoint | None:
    """The endpoint a document at ``path`` contributes, or None.

    The legacy `documents` catalog is written only by the bare-Git path; on
    `postgres_native` the native arm writes `native_resources` and never
    touches it (`document_service.newest_public_slug` — "the native-ledger arm
    keeps no legacy projection"). Asking the catalog alone therefore answers
    "no such document" for everything written after a vault's cutover, which
    made `akb_link` reject a freshly created document as a missing endpoint
    while `akb_get` served it — and silently dropped the body-link edges that
    `_store_edge` extracts from it.

    Both arms are asked on a native installation rather than only the
    authoritative one, because a cutover leaves the pre-cutover documents in
    the catalog: an endpoint that resolved yesterday must not stop resolving.
    `lifecycle` matches `document_counters`' population — a deleted resource is
    not an endpoint; an archived one is still live here.
    """
    if await conn.fetchval(_DOC_EXISTS_LEGACY, vault_id, path):
        return DocumentEndpoint(path=path, resource_id=None)
    from app.services.document_counters import native_documents_are_authoritative

    if not native_documents_are_authoritative():
        return None
    row = await conn.fetchrow(_NATIVE_DOC_BY_PATH, vault_id, path)
    if row is None:
        return None
    return DocumentEndpoint(path=row["current_path"], resource_id=row["resource_id"])


async def _resolve_document_reference(
    conn, vault_id: uuid.UUID, ref: str,
) -> DocumentEndpoint | None:
    """Resolve a NON-URI document reference through the active authority.

    A body's `[B](a.md)` / `[[a.md]]` and a frontmatter `depends_on: [a.md]`
    all arrive here. Before this, they were resolved against `documents`
    alone — so on a native installation every such reference to a
    post-cutover document produced no edge at all, silently, while the same
    target written as `akb://V/doc/a.md` linked fine (akb#656).

    The legacy arm is asked first and is untouched: a legacy installation
    never reaches the native lookup, and a native installation still resolves
    its pre-cutover documents exactly as before.
    """
    ref = normalize_document_link_ref(ref)
    legacy_id = await _resolve_doc_ref(conn, vault_id, ref)
    if legacy_id is not None:
        path = await conn.fetchval(
            "SELECT path FROM documents WHERE id = $1", legacy_id,
        )
        if path:
            return DocumentEndpoint(path=path, resource_id=None)

    from app.services.document_counters import native_documents_are_authoritative

    if not native_documents_are_authoritative():
        return None

    row = await conn.fetchrow(_NATIVE_DOC_BY_REF, vault_id, ref)
    if row is not None:
        return DocumentEndpoint(
            path=row["current_path"], resource_id=row["resource_id"],
        )

    # Trailing-segment match, anchored at `/` exactly as the legacy arm is.
    # Unlike the legacy arm this one REFUSES an ambiguous reference instead of
    # returning whichever row came back first: the legacy behaviour is kept
    # because callers depend on it, but it is a wrong-doc magnet its own
    # docstring admits to, and a new arm should not inherit that.
    rows = await conn.fetch(_NATIVE_DOC_BY_SUFFIX, vault_id, like_escape(ref))
    if len(rows) == 1:
        return DocumentEndpoint(
            path=rows[0]["current_path"], resource_id=rows[0]["resource_id"],
        )
    if len(rows) > 1:
        logger.debug(
            "Ambiguous native document reference %r — no edge stored", ref,
        )
    return None


async def _document_exists(conn, vault_id: uuid.UUID, path: str) -> bool:
    """True when either authority serves a document at ``path``.

    One population definition, shared with `_resolve_document_endpoint`, so
    the existence check and the edge the extraction writes cannot disagree
    about which documents are endpoints.
    """
    return await _resolve_document_endpoint(conn, vault_id, path) is not None


async def _resource_exists(conn, vault_id: uuid.UUID, rtype: str, identifier: str) -> bool:
    """Check if a resource actually exists in the database."""
    if rtype == "doc":
        return await _document_exists(conn, vault_id, identifier)
    elif rtype == "table":
        return bool(await conn.fetchval(
            "SELECT 1 FROM vault_tables WHERE vault_id = $1 AND name = $2", vault_id, identifier,
        ))
    elif rtype == "file":
        return bool(await conn.fetchval(
            "SELECT 1 FROM vault_files f WHERE vault_id = $1 AND "
            + confirmed_file_predicate("f") + " AND id::text = $2",
            vault_id,
            identifier,
        ))
    return False


async def _store_edge(
    conn, vault_id: uuid.UUID, vault_name: str,
    source_uri: str, source_type: str,
    target_ref: str, relation_type: str,
    source_resource_id: uuid.UUID | None = None,
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
        if parsed.vault != vault_name:
            # Cross-vault Markdown links remain authored content, but are not
            # graph edges. Rejecting before any vault lookup also prevents the
            # extraction result from revealing whether the target exists.
            logger.debug(
                "Skipping cross-vault implicit edge from vault %r", vault_name,
            )
            return False
        target_type = parsed.kind
        ident = parsed.identifier or ""
        target_resource_id: uuid.UUID | None = None
        # Validate the target EXISTS before storing — through the SAME
        # resolution primitive the explicit akb_link path uses, so the two
        # link paths validate identically (they differ only in policy:
        # akb_link returns NOT_FOUND, extraction silently skips).
        # An implicit edge to a non-existent resource can never be drawn and
        # only pollutes the graph — e.g. a wikilink whose alias leaked into
        # the path (`…/x.md|Label`), or a forward reference to a doc never
        # created. The extraction path used to skip this check, which is how
        # the malformed targets got persisted.
        if parsed.kind == "doc":
            endpoint = await _resolve_document_endpoint(conn, vault_id, ident)
            if endpoint is None:
                logger.debug(
                    "Skipping edge to nonexistent %s %r", parsed.kind, target_ref,
                )
                return False
            # Rebuild from the resolved path so surface variants collapse under
            # the edges uniqueness convention — otherwise ON CONFLICT can't
            # dedupe.
            target_uri = doc_uri(parsed.vault, endpoint.path)
            target_resource_id = endpoint.resource_id
        else:
            if not await _resource_exists(conn, vault_id, parsed.kind, ident):
                logger.debug(
                    "Skipping edge to nonexistent %s %r", parsed.kind, target_ref,
                )
                return False
            if parsed.kind == "table":
                target_uri = table_uri(parsed.vault, ident, parsed.coll_path)
            else:
                target_uri = file_uri(parsed.vault, ident, parsed.coll_path)
    else:
        # Non-URI reference: a body path, a wiki link, or a bare frontmatter
        # ref. Resolved through the ACTIVE AUTHORITY — asking `documents`
        # alone is what left every native-only target unlinked (akb#656).
        endpoint = await _resolve_document_reference(conn, vault_id, target_ref)
        if endpoint is None:
            return False
        target_uri = doc_uri(vault_name, endpoint.path)
        target_type = "doc"
        target_resource_id = endpoint.resource_id

    if source_uri == target_uri:
        return False

    await conn.execute(
        """
        INSERT INTO edges (id, vault_id, source_uri, target_uri, relation_type,
                           source_type, target_type,
                           source_resource_id, target_resource_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        ON CONFLICT (source_uri, target_uri, relation_type) DO UPDATE
        SET source_resource_id = EXCLUDED.source_resource_id,
            target_resource_id = EXCLUDED.target_resource_id
        """,
        uuid.uuid4(), vault_id, source_uri, target_uri, relation_type,
        source_type, target_type, source_resource_id, target_resource_id,
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
      3. Trailing-segment match — `path LIKE '%/' || $2` with the
         reference escaped as a literal pattern (e.g. ref `api.md`
         matches `notes/api.md` iff there is exactly one such doc; the
         unique constraint on `(vault_id, path)` does NOT dedupe across
         collections, so this can still return one of several matches —
         but the suffix is anchored at `/`, so `api.md` cannot match
         `funapi.md`).
    """
    ref = normalize_document_link_ref(ref)

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
        "SELECT id FROM documents WHERE vault_id = $1 AND path LIKE '%/' || $2 ESCAPE '\\'",
        vault_id, like_escape(ref),
    )
    if row:
        return row["id"]

    return None


async def _bfs_collect(
    conn, vault_id: uuid.UUID, vault_name: str, start_uri: str,
    depth: int, limit: int,
    nodes: dict[str, dict], edges: list[dict],
    *, edge_type: str | None = None, direction: str | None = None,
) -> bool:
    """BFS traversal from a starting resource URI, scoped to one vault.

    Edge ownership and both endpoint URI authorities must match the requested
    vault. A legacy cross-vault edge therefore contributes no node, row, or
    traversal seed.

    ``edge_type`` ("strong" / "weak", ``None`` = every edge) is applied in the
    edge fetch itself, to the outgoing and the incoming query alike, so the walk
    never crosses an edge of the other strength: a neighbour reachable only
    through one is not visited, and the ``limit`` node cap is spent on the
    neighbours that qualify.

    ``direction`` ("both" or ``None`` = every edge of a node; "outward" = its
    outgoing edges plus the incoming ``derived_from`` and ``depends_on`` ones,
    :data:`OUTWARD_INCOMING_RELATIONS`) narrows the *incoming* fetch the same
    way, so a node that is only pointed at by other relations — a hub — is
    never walked through and never spends the cap. Edges are emitted as the
    walk follows them: with ``outward`` they are the outgoing edges and the
    incoming ``derived_from`` / ``depends_on`` edges of the nodes it expanded,
    and an edge it did not follow is absent even when both its ends were
    visited.

    Returns whether ``limit`` cut the walk short — True when a node that
    qualified and lies within ``depth`` hops could not be added because the cap
    was already spent. Reaching ``depth`` is not truncation, and neither is a
    result that fits ``limit`` exactly.
    """
    queue = [start_uri]
    visited: set[str] = set()
    truncated = False
    # Track emitted edges so an edge A→B doesn't appear twice when both
    # A and B are processed in the same BFS wave.
    emitted: set[tuple[str, str, str]] = set()
    prefix = vault_uri_prefix(vault_name)
    scoped = edge_scope_sql(vault_param=2, prefix_param=3)
    strength = edge_strength_sql(edge_type)
    if strength:
        scoped = f"{scoped} AND {strength}"
    # The direction narrows only the incoming fetch; an outgoing edge is
    # followed in every mode. Built after the strength so a bad `direction`
    # raises before the first query, like a bad `edge_type`.
    scoped_incoming = scoped
    inward = incoming_walk_sql(direction)
    if inward:
        scoped_incoming = f"{scoped} AND {inward}"

    for current_depth in range(depth + 1):
        if not queue:
            break

        next_queue: list[str] = []

        # `dict.fromkeys` keeps discovery order and drops repeats: a node
        # reached from two parents in one wave is queued once per parent.
        unvisited = [u for u in dict.fromkeys(queue) if u not in visited]
        if not unvisited:
            break
        if len(nodes) >= limit:
            # The cap is spent and these are still waiting to be added.
            truncated = True
            break

        outgoing = await conn.fetch(
            "SELECT source_uri, target_uri, target_type, relation_type, kind, "
            f"{_STORED_EDGE_TYPE_COLUMN} "
            f"FROM edges WHERE source_uri = ANY($1::text[]) AND {scoped}",
            unvisited, vault_id, prefix,
        )
        incoming = await conn.fetch(
            "SELECT source_uri, source_type, target_uri, relation_type, kind, "
            f"{_STORED_EDGE_TYPE_COLUMN} "
            f"FROM edges WHERE target_uri = ANY($1::text[]) AND {scoped_incoming}",
            unvisited, vault_id, prefix,
        )
        outgoing = [
            row for row in outgoing
            if uri_is_in_vault(row["source_uri"], vault_name)
            and uri_is_in_vault(row["target_uri"], vault_name)
        ]
        incoming = [
            row for row in incoming
            if uri_is_in_vault(row["source_uri"], vault_name)
            and uri_is_in_vault(row["target_uri"], vault_name)
        ]

        # Index edges by source/target for quick lookup
        out_by_uri: dict[str, list] = {}
        for r in outgoing:
            out_by_uri.setdefault(r["source_uri"], []).append(r)
        in_by_uri: dict[str, list] = {}
        for r in incoming:
            in_by_uri.setdefault(r["target_uri"], []).append(r)

        for uri in unvisited:
            if len(nodes) >= limit:
                truncated = True
                break
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
                        "edge_type": resolve_edge_type(r["kind"], r["stored_edge_type"]),
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
                        "edge_type": resolve_edge_type(r["kind"], r["stored_edge_type"]),
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
    labels = await _batch_resolve_labels(
        conn, uris_to_resolve, vault_id=vault_id, vault_name=vault_name,
    )
    for uri, node in nodes.items():
        node.update(labels.get(uri, {}))
    return truncated


# ── URI builder helpers for callers ──────────────────────────
