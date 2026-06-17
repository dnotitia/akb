"""AKB MCP Tool Definitions.

All tool schemas for the AKB MCP server.

Identifier policy
-----------------
Clients never see internal database IDs (UUIDs, prefixed `d-…` ids,
file UUIDs, table UUIDs). The single canonical handle for every vault
resource is the AKB URI — location-aware as of 0.3.0:

  - akb://{vault}                                            vault root
  - akb://{vault}/coll/{coll_path}                           collection
  - akb://{vault}[/coll/{coll_path}]/doc/{filename}          document
  - akb://{vault}[/coll/{coll_path}]/table/{name}            table
  - akb://{vault}[/coll/{coll_path}]/file/{uuid}             file

The `/coll/{coll_path}` segment is omitted for resources at the vault
root. ``{filename}`` is the document's basename (no slashes); the
collection lives in the `/coll/...` segment. ``{name}`` is the table
name (unique per vault); ``{uuid}`` is the file's UUID PK.

Tools that target an existing resource take `uri` and nothing else
(vault is encoded in the URI). Tools that *create* a new resource keep
`vault` + identity bits (title, collection, name) since no URI exists
yet — the returned payload then carries the newly-minted `uri`.

The pair tools (`akb_link`, `akb_unlink`) take `source` + `target`
URIs — they too do not need a separate `vault` arg.

Opaque domain handles that are *not* vault resources keep their own
ID parameter (`todo_id`, publication `slug`); these are not
URI-addressable.
"""

from mcp.types import Tool

from app.services import template_registry
from app.services.kg_service import LINK_RELATION_TYPES

# The link/unlink relation vocabulary is defined once in kg_service; the
# tool schemas below derive from it so the MCP surface and the REST
# `RelationType` model can never drift. `_REL_LIST` is the human-readable
# spelling reused across descriptions.
_REL_ENUM = list(LINK_RELATION_TYPES)
_REL_LIST = ", ".join(LINK_RELATION_TYPES)


TOOLS = [
    Tool(
        name="akb_list_vaults",
        description=(
            "List accessible vaults as {name, description} pairs. "
            "Response is slim — no metadata (id/role/created_at) — to "
            "fit large tenants in agent context. Returns "
            "{vaults, total, returned, truncated?, hint?}.\n"
            "Optional args:\n"
            "- filter: substring match on name+description (case-insensitive). "
            "Use to narrow to a domain (e.g. filter='finance').\n"
            "- limit / offset: pagination when there are many matches.\n"
            "- include_archived: include archived vaults (default false)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "filter": {"type": "string", "description": "Substring filter against name+description (case-insensitive)."},
                "limit": {"type": "integer", "description": "Cap result count."},
                "offset": {"type": "integer", "description": "Skip first N (default 0)."},
                "include_archived": {"type": "boolean", "description": "Include archived vaults (default false)."},
            },
        },
    ),
    Tool(
        name="akb_create_vault",
        description=(
            "Create a new knowledge base vault (a separate, access-controlled repository for documents). "
            "Pass `external_git` to instead create a read-only mirror of an upstream git repo — the vault "
            "tracks the remote on a polling schedule and rejects user writes."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Vault name (lowercase, hyphens allowed)"},
                "description": {"type": "string", "description": "What this vault is for"},
                "template": {
                    "type": "string",
                    "enum": template_registry.list_names(),
                    "description": (
                        "Vault template to apply (pre-creates collections with guides). "
                        "Ignored when external_git is set."
                    ),
                },
                "public_access": {"type": "string", "enum": ["none", "reader", "writer"], "default": "none", "description": "Public access: none=private, reader=public read, writer=public read+write"},
                "external_git": {
                    "type": "object",
                    "description": "Optional: turn the new vault into a read-only mirror of an upstream git repo.",
                    "properties": {
                        "url": {"type": "string", "description": "HTTPS clone URL of the upstream repo"},
                        "branch": {"type": "string", "default": "main", "description": "Upstream branch to track"},
                        "auth_token": {"type": "string", "description": "Optional PAT for private repos. Stored in DB; rotate via vault admin."},
                        "poll_interval_secs": {"type": "integer", "default": 300, "minimum": 60, "description": "Seconds between upstream polls"},
                    },
                    "required": ["url"],
                },
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="akb_put",
        description=(
            "Store a new document. The response carries the canonical `uri` — "
            "`akb://{vault}/coll/{collection}/doc/{filename}` when stored under a "
            "collection, or `akb://{vault}/doc/{filename}` at the vault root. Use "
            "that URI to address the document from every other tool. Automatically "
            "chunked and indexed for semantic search."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "parent": {
                    "type": "string",
                    "description": (
                        "Parent location as a canonical URI — `akb://{vault}` "
                        "for the vault root, `akb://{vault}/coll/{path}` for a "
                        "collection. When given, the doc is placed there and "
                        "`vault`/`collection` are derived from the URI. Use "
                        "this in drill-down chains: paste the `uri` from an "
                        "`akb_browse` response straight back in."
                    ),
                },
                "vault": {"type": "string", "description": "Target vault name. Required unless `parent` is given."},
                "collection": {"type": "string", "description": "Collection (directory) path, e.g. 'api-specs' or 'meeting-notes'. Ignored when `parent` is given."},
                "slug": {"type": "string", "description": "Optional explicit slug for the document filename. When stored under a collection the URI is `akb://{vault}/coll/{collection}/doc/{slug}.md`; at the vault root it is `akb://{vault}/doc/{slug}.md`. When omitted, the slug is derived from the title. Pass it to keep the path stable and meaningful when the title is friendly, changeable text (e.g. slug `github-issue-123` with a human-readable title)."},
                "title": {"type": "string", "description": "Document title"},
                "content": {"type": "string", "description": "Document body in Markdown"},
                "type": {
                    "type": "string",
                    "description": "Document type",
                    "enum": ["note", "report", "decision", "spec", "plan", "session", "task", "reference", "skill"],
                    "default": "note",
                },
                "status": {
                    "type": "string",
                    "description": (
                        "Lifecycle status. Defaults to 'draft'; pass 'active' to publish on "
                        "create instead of promoting later with akb_update. Descriptive "
                        "metadata only — it does not gate search, browse, or access."
                    ),
                    "enum": ["draft", "active", "archived"],
                    "default": "draft",
                },
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags for classification"},
                "domain": {"type": "string", "description": "Domain: engineering, product, ops, legal, etc."},
                "summary": {"type": "string", "description": "Brief summary (auto-generated if omitted)"},
                "depends_on": {"type": "array", "items": {"type": "string"}, "description": "akb:// URIs this depends on"},
                "related_to": {"type": "array", "items": {"type": "string"}, "description": "akb:// URIs of related resources"},
            },
            # `vault` + `collection` are required-via-handler rather than
            # required-in-schema — passing `parent` instead also satisfies.
            "required": ["title", "content"],
        },
    ),
    Tool(
        name="akb_get",
        description=(
            "Retrieve a document by its URI. Returns full content with metadata. "
            "Use akb_browse or akb_search first to obtain the URI. "
            "Optionally pass a commit hash (from akb_history) to read a previous version."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Document URI — akb://{vault}[/coll/{coll_path}]/doc/{filename}"},
                "version": {"type": "string", "description": "Git commit hash for a specific version (from akb_history)"},
            },
            "required": ["uri"],
        },
    ),
    Tool(
        name="akb_update",
        description="Update an existing document. Only provide fields you want to change.",
        inputSchema={
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Document URI"},
                "content": {"type": "string", "description": "New document body (replaces existing)"},
                "title": {"type": "string", "description": "New title"},
                "status": {"type": "string", "enum": ["draft", "active", "archived"]},
                "tags": {"type": "array", "items": {"type": "string"}},
                "summary": {"type": "string"},
                "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Update dependency list (akb:// URIs)"},
                "related_to": {"type": "array", "items": {"type": "string"}, "description": "Update related list (akb:// URIs)"},
                "message": {"type": "string", "description": "Commit message describing the change"},
                "expected_commit": {
                    "type": "string",
                    "description": "Optional OCC pin — reject if the document current_commit moved.",
                },
                "expected_content_hash": {
                    "type": "string",
                    "description": "Optional body hash pin — reject if the current document body hash moved.",
                },
            },
            "required": ["uri"],
        },
    ),
    Tool(
        name="akb_edit",
        description=(
            "Edit a single document by replacing exact text. Scope is one document. "
            "old_string must be unique within the document (or use replace_all). "
            "If old_string is not found or appears multiple times, the call fails with a clear error. "
            "For find-and-replace across many documents, use akb_grep with replace instead."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Document URI"},
                "old_string": {
                    "type": "string",
                    "description": "Exact text to replace. Must be unique in the document body unless replace_all=true. Include surrounding context if needed for uniqueness.",
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text. Can be empty to delete.",
                },
                "replace_all": {
                    "type": "boolean",
                    "default": False,
                    "description": "Replace all occurrences (default: false, requires old_string to be unique)",
                },
                "message": {"type": "string", "description": "Commit message describing the change"},
                "base_commit": {
                    "type": "string",
                    "description": "Optional OCC pin — when set, the edit is rejected if the document's current_commit moved. Use after akb_get to fail-fast on concurrent writers.",
                },
            },
            "required": ["uri", "old_string", "new_string"],
        },
    ),
    Tool(
        name="akb_move",
        description=(
            "Move or rename a document — change its collection and/or slug while "
            "keeping its identity and full git history. The old akb:// URI keeps "
            "resolving (a redirect is recorded), and graph links/publications are "
            "rewritten. Provide collection and/or slug (at least one must change). "
            "The title is unchanged; use akb_update to change the displayed title."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Document URI to move"},
                "collection": {
                    "type": "string",
                    "description": "New collection path (omit to keep the current collection)",
                },
                "slug": {
                    "type": "string",
                    "description": "New slug / filename base, e.g. 'final-spec' (omit to keep the current slug)",
                },
                "message": {"type": "string", "description": "Commit message describing the move"},
            },
            "required": ["uri"],
        },
    ),
    Tool(
        name="akb_delete",
        description="Delete a document. Removes from Git, search index, and knowledge graph.",
        inputSchema={
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Document URI"},
            },
            "required": ["uri"],
        },
    ),
    Tool(
        name="akb_browse",
        description=(
            "Browse ALL vault content — documents, tables, and files — under a browse "
            "root. The browse root can be addressed two ways: pass a canonical `uri` "
            "(`akb://V` for vault root or `akb://V/coll/X` for a collection), or the "
            "legacy `vault` + optional `collection` pair. Use the URI form when "
            "drilling down from a previous response — every item carries a `uri` "
            "that can be pasted straight back in.\n\n"
            "`depth` is tree-depth from the browse root, mirroring `tree -L N`: "
            "0 = direct children only (no descent), N = descend N collection levels, "
            "-1 = entire subtree. Collection rows are always emitted as navigation "
            "aids regardless of depth.\n\n"
            "Response is slim by default (no `summary` field) so large vaults "
            "(70+ collections) fit in the agent's context window. Returns "
            "{vault, path, items, total, returned, truncated?, hint?}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "uri": {
                    "type": "string",
                    "description": (
                        "Canonical browse target: `akb://{vault}` (vault root) or "
                        "`akb://{vault}/coll/{path}` (collection-scoped). Takes "
                        "precedence over `vault` + `collection` when both are given."
                    ),
                },
                "vault": {
                    "type": "string",
                    "description": "Vault name. Required unless `uri` is given.",
                },
                "collection": {
                    "type": "string",
                    "description": (
                        "Collection path to use as the browse root (omit for "
                        "vault root). Ignored when `uri` is given."
                    ),
                },
                "depth": {
                    "type": "integer",
                    "description": (
                        "Tree depth from the browse root. 0 = direct children only "
                        "(no descent into any collection). N = descend N collection "
                        "levels. -1 = unbounded (entire subtree). Collections "
                        "themselves are always emitted regardless of depth."
                    ),
                    "default": 1,
                    "minimum": -1,
                },
                "content_type": {
                    "type": "string",
                    "enum": ["all", "documents", "tables", "files"],
                    "default": "all",
                    "description": "Filter by content type",
                },
                "filter": {"type": "string", "description": "Substring filter on item name/path (case-insensitive)."},
                "limit": {"type": "integer", "description": "Cap returned item count."},
                "offset": {"type": "integer", "description": "Skip first N items (default 0)."},
                "include_summary": {"type": "boolean", "description": "Include the per-item summary field (default false, drops to keep payload small)."},
                "include_hashes": {
                    "type": "boolean",
                    "description": (
                        "Include AKB-certified content_hash/hash_algorithm and "
                        "resource version fields for documents/files."
                    ),
                },
                "include_archived": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include archived documents. Default false — `status: archived` docs are hidden from browse.",
                },
            },
            # Either `vault` or `uri` must be present — enforced at the
            # handler since JSON schema's anyOf-on-required is awkward
            # to express in some MCP clients.
        },
    ),
    Tool(
        name="akb_search",
        description=(
            "Search documents with hybrid retrieval — dense vector (semantic) fused with "
            "BM25 sparse (keyword) via Reciprocal Rank Fusion. Handles both natural-language "
            "questions and short keyword queries well. For exact string / regex matches "
            "(code, URLs, version numbers) prefer akb_grep. Returns each hit's `uri`; "
            "use akb_drill_down or akb_get with that URI for full content. "
            "Response reports `returned` (in `results`) and `total_matches` (size of the "
            "deduped prefetch pool — NOT a corpus-wide hit count; vector ANN is top-K only). "
            "When `truncated=true` the prefetch pool was capped, meaning the corpus may hold "
            "more hits than reported — switch to akb_grep with count_only=true for an exact "
            "literal-substring count, or refine the query. "
            "When `degraded=true` the retrieval index hit a transient failure (vector-store "
            "outage or a degraded leg), so results may be incomplete or empty — this is NOT a "
            "genuine zero-match; `degradation_reason` names the cause. Retry shortly, or fall "
            "back to akb_grep for a literal search."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language search query"},
                "vault": {"type": "string", "description": "Limit search to a specific vault"},
                "collection": {"type": "string", "description": "Limit search to a specific collection"},
                "type": {
                    "type": "string",
                    "description": "Filter by document type",
                    "enum": ["note", "report", "decision", "spec", "plan", "session", "task", "reference", "skill"],
                },
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Filter by tags"},
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                "include_archived": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include archived documents. Default false — `status: archived` docs are hidden from search.",
                },
                "source_uris": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Restrict the search to a specific set of already-known resources "
                        "by their canonical akb:// URIs (e.g. from a previous akb_search / "
                        "akb_browse). Hybrid retrieval (dense + BM25 + ranking) runs only "
                        "inside this set, intersected with the other filters and your access. "
                        "Omit for the normal whole-vault search."
                    ),
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="akb_grep",
        description=(
            "Search for exact text or regex patterns across document content. "
            "Unlike akb_search (semantic/meaning-based), this finds exact string matches — "
            "use it for specific terms, URLs, code snippets, version numbers, etc. "
            "Returns matching documents (each with its `uri`) and matched lines. "
            "Optionally pass `replace` to find-and-replace across all matching documents. "
            "Three response shapes (mutually exclusive): default lines, `count_only=true` "
            "(grep -c — per-doc counts + total, no snippets), `files_with_matches=true` "
            "(grep -l — just the URIs that contain the pattern). "
            "The default shape always reports BOTH `returned_*` (what fit under `limit`) "
            "and `total_*` (full corpus matches) plus a `truncated` flag — if truncated, "
            "switch to count_only/files_with_matches for the full picture instead of bumping `limit`."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Search pattern. By default matched as literal text (ILIKE) — metacharacters like |, ., *, (), [], +, ? are treated as literal characters. Set regex=true to enable PostgreSQL regex (required for alternation and wildcards)."},
                "vault": {"type": "string", "description": "Limit to a specific vault"},
                "collection": {"type": "string", "description": "Limit to a specific collection"},
                "regex": {"type": "boolean", "default": False, "description": "Treat pattern as PostgreSQL regex. REQUIRED to use alternation (|), wildcards (.*), character classes, anchors, etc. When false (default), the entire pattern including any metacharacters is matched literally."},
                "case_sensitive": {"type": "boolean", "default": False, "description": "Case-sensitive matching (default: case-insensitive)"},
                "replace": {"type": "string", "description": "Replacement string. If provided, replaces all matches in EVERY matching document across the search scope (git commit + re-index per doc). Supports regex backreferences (\\1, \\2) when regex=true. For precise edits to a single known document, prefer akb_edit instead."},
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 50, "description": "Max documents to return"},
                "count_only": {"type": "boolean", "default": False, "description": "Return counts only (grep -c semantics). Response: {pattern, total_matches, total_docs, by_doc:{uri:count,...}}. Use for 'how many X are there?' questions — much cheaper than fetching every line."},
                "files_with_matches": {"type": "boolean", "default": False, "description": "Return only the URIs that contain matches (grep -l semantics). Response: {pattern, n_files, files:[uri,...]}. Use for 'which documents mention X?' questions."},
            },
            "required": ["pattern"],
        },
    ),
    Tool(
        name="akb_drill_down",
        description=(
            "Read section-level (L3) content of a document, or list its "
            "section headings.\n"
            "Two modes:\n"
            "- `mode='sections'` (default): return body content of matched "
            "sections. Filter with `section` (heading substring) and/or "
            "`pattern` (substring grep on body). On empty match the response "
            "carries an `outline` so you can retry.\n"
            "- `mode='outline'`: return heading paths only (no bodies). "
            "Use this to discover the document's structure cheaply before "
            "deciding which section to read.\n"
            "Returns {uri, sections|outline, returned, total?, truncated?, hint?}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Document URI"},
                "mode": {
                    "type": "string",
                    "enum": ["sections", "outline"],
                    "default": "sections",
                    "description": "'sections' for body content, 'outline' for heading paths only.",
                },
                "section": {"type": "string", "description": "Section heading filter (partial match). Used in `sections` mode."},
                "pattern": {"type": "string", "description": "Substring grep inside matched section bodies (case-insensitive). Used in `sections` mode."},
            },
            "required": ["uri"],
        },
    ),
    Tool(
        name="akb_activity",
        description=(
            "Get activity history for a vault — who changed what, when, and why. "
            "Returns Git commit history with changed file list. "
            "Use akb_diff to see the actual content changes for a specific commit."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Vault name"},
                "collection": {"type": "string", "description": "Filter by collection path prefix"},
                "author": {"type": "string", "description": "Filter by author name"},
                "since": {"type": "string", "description": "ISO datetime to filter from (e.g. 2026-04-01)"},
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
            },
            "required": ["vault"],
        },
    ),
    Tool(
        name="akb_diff",
        description=(
            "Get the content diff for a document at a specific commit. "
            "Shows what was added/removed/modified. "
            "Use akb_history or akb_activity to find commit hashes first."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Document URI"},
                "commit": {"type": "string", "description": "Commit hash (from akb_history or akb_activity)"},
            },
            "required": ["uri", "commit"],
        },
    ),
    Tool(
        name="akb_relations",
        description=(
            "Get relations for any resource (document, table, or file). "
            "Shows cross-type connections: doc→table, doc→file, table→file, etc."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Resource URI (akb://vault/doc/path, akb://vault/table/name, akb://vault/file/uuid)"},
                "direction": {"type": "string", "enum": ["incoming", "outgoing", "both"], "default": "both"},
                "type": {"type": "string", "description": f"Filter by relation type ({_REL_LIST})"},
            },
            "required": ["uri"],
        },
    ),
    Tool(
        name="akb_graph",
        description=(
            "Get a knowledge graph — nodes (documents, tables, files) and edges (relations). "
            "Provide `uri` to get a subgraph centered on any resource with BFS traversal. "
            "Provide `vault` (without uri) to get the full vault graph."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Center resource URI (omit + pass vault for full vault graph)"},
                "vault": {"type": "string", "description": "Vault name (only when uri is omitted — for full vault graph)"},
                "hops": {
                    "type": "integer",
                    "default": 2,
                    "minimum": 1,
                    "maximum": 5,
                    "description": (
                        "BFS traversal radius in edge hops. Disambiguated from "
                        "`akb_browse.depth` (which is collection-tree depth) — "
                        "hops here counts relations followed, not folder levels."
                    ),
                },
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200, "description": "Max nodes"},
            },
        },
    ),
    Tool(
        name="akb_link",
        description=(
            "Create a relation between any two resources (documents, tables, files). "
            "Source and target are AKB URIs. "
            f"Relation types: {_REL_LIST}. "
            "Example: link a design doc to its data table, or attach a diagram file to a spec."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Source resource URI (e.g. akb://vault/doc/specs/api.md)"},
                "target": {"type": "string", "description": "Target resource URI (e.g. akb://vault/table/experiments)"},
                "relation": {
                    "type": "string",
                    "description": "Relation type",
                    "enum": _REL_ENUM,
                },
            },
            "required": ["source", "target", "relation"],
        },
    ),
    Tool(
        name="akb_unlink",
        description=(
            "Remove a relation between two resources. "
            "If relation type is omitted, removes ALL relations between the two resources."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Source resource URI"},
                "target": {"type": "string", "description": "Target resource URI"},
                "relation": {
                    "type": "string",
                    "description": f"Specific relation type to remove, one of: {_REL_LIST} (omit to remove all)",
                    "enum": _REL_ENUM,
                },
            },
            "required": ["source", "target"],
        },
    ),
    Tool(
        name="akb_provenance",
        description="Get provenance for a document — who created it, when, which entities were extracted.",
        inputSchema={
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Document URI"},
            },
            "required": ["uri"],
        },
    ),
    Tool(
        name="akb_create_table",
        description=(
            "Create a structured data table in a vault. The response carries the "
            "canonical `uri` — `akb://{vault}/coll/{collection}/table/{name}` when "
            "stored under a collection, or `akb://{vault}/table/{name}` at the vault "
            "root. Tables live alongside documents inside collections and follow the "
            "same permissions. Define columns with name and type (text, number, "
            "boolean, date, json). Optional `collection` (e.g. 'sessions/learnings') "
            "groups the table under that collection so it appears beside the documents "
            "and files there in akb_browse; omit for vault root."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "parent": {
                    "type": "string",
                    "description": (
                        "Parent location as a canonical URI — `akb://{vault}` "
                        "for the vault root, `akb://{vault}/coll/{path}` for a "
                        "collection. When given, the table is created there "
                        "and `vault`/`collection` are derived from the URI."
                    ),
                },
                "vault": {"type": "string", "description": "Target vault name. Required unless `parent` is given."},
                "name": {"type": "string", "description": "Table name (unique within the vault)"},
                "collection": {
                    "type": "string",
                    "description": "Collection path (e.g. 'specs' or 'sessions/learnings'). Omit for vault root. Ignored when `parent` is given.",
                },
                "description": {"type": "string"},
                "columns": {
                    "type": "array",
                    "description": "Column definitions",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "type": {"type": "string", "enum": ["text", "number", "boolean", "date", "json"]},
                            "required": {"type": "boolean", "default": False},
                        },
                        "required": ["name", "type"],
                    },
                },
            },
            "required": ["name", "columns"],
        },
    ),
    Tool(
        name="akb_sql",
        description=(
            "Execute SQL on vault tables. Tables are real PostgreSQL tables. "
            "Use table names directly (e.g. 'pipeline', 'partners') — they are auto-resolved to the vault's tables. "
            "For cross-vault queries, list all vaults in the vaults parameter. "
            "Prefix table names with vault name for cross-vault: sales__pipeline, external_projects__partners. "
            "SELECT requires reader role. INSERT/UPDATE/DELETE requires writer role."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "SQL query to execute"},
                "vaults": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Vault names whose tables are referenced (default: single vault)",
                },
                "vault": {"type": "string", "description": "Single vault shorthand (instead of vaults array)"},
            },
            "required": ["sql"],
        },
    ),
    Tool(
        name="akb_drop_table",
        description="Permanently delete a table and all its rows. Cannot be undone. Requires admin role on the vault.",
        inputSchema={
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Table URI — akb://{vault}[/coll/{coll_path}]/table/{name}"},
            },
            "required": ["uri"],
        },
    ),
    Tool(
        name="akb_alter_table",
        description="Modify a table's schema — add, remove, or rename columns via ALTER TABLE DDL. Requires admin role.",
        inputSchema={
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Table URI — akb://{vault}[/coll/{coll_path}]/table/{name}"},
                "add_columns": {
                    "type": "array",
                    "description": "Columns to add",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "type": {"type": "string", "enum": ["text", "number", "boolean", "date", "json"]},
                        },
                        "required": ["name", "type"],
                    },
                },
                "drop_columns": {
                    "type": "array",
                    "description": "Column names to remove",
                    "items": {"type": "string"},
                },
                "rename_columns": {
                    "type": "object",
                    "description": "Rename columns: {old_name: new_name}",
                },
            },
            "required": ["uri"],
        },
    ),
    Tool(
        name="akb_publish",
        description=(
            "Create a public, no-auth share URL for a document, file, or table query. "
            "Document/file: pass the resource `uri`. Table query: pass `query_sql` "
            "plus `vault` (and `query_vault_names` if the query touches more than one). "
            "Returns the canonical publication dict — `slug` is the only identifier "
            "you need; `share_url` is always an absolute URL ready to paste."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Resource URI to publish — required when resource_type is document or file. Omit for table_query."},
                "resource_type": {
                    "type": "string",
                    "enum": ["document", "table_query", "file"],
                    "default": "document",
                    "description": "Kind of resource. document/file → pass `uri`. table_query → pass `query_sql` + `vault`.",
                },
                "vault": {"type": "string", "description": "Vault name. Required only for resource_type=table_query (doc/file vault is inferred from the URI)."},
                "query_sql": {
                    "type": "string",
                    "description": "SELECT/WITH SQL with :param placeholders. resource_type=table_query only.",
                },
                "query_vault_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Vaults the query reads from. Defaults to [vault]. resource_type=table_query only.",
                },
                "query_params": {
                    "type": "object",
                    "description": "Parameter declarations: {name: {type, default, required}}. resource_type=table_query only.",
                },
                "password": {"type": "string", "description": "Require this password to view the share."},
                "max_views": {"type": "integer", "description": "Auto-expire after N views."},
                "expires_in": {
                    "type": "string",
                    "description": "Expiration window: '1h', '7d', '30d', or 'never' (default).",
                },
                "title": {"type": "string", "description": "Override the display title (defaults to the resource's own title)."},
                "section_filter": {
                    "type": "string",
                    "description": "Filter to a specific heading section. resource_type=document only.",
                },
                "allow_embed": {
                    "type": "boolean",
                    "default": True,
                    "description": "Allow the share to be embedded via iframe/oEmbed.",
                },
            },
        },
    ),
    Tool(
        name="akb_unpublish",
        description=(
            "Remove publication(s). Pass `slug` to remove one specific publication, "
            "OR `uri` to remove every publication of that document/file resource "
            "(handy when re-publishing). table_query publications have no resource "
            "URI, so remove them by slug. Returns {deleted: N}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Publication slug — remove exactly this publication."},
                "uri": {"type": "string", "description": "Document or file URI — remove every publication tied to that resource."},
            },
        },
    ),
    Tool(
        name="akb_publications",
        description=(
            "List every publication in a vault. Each item is the canonical "
            "publication dict (same shape as `akb_publish` returns)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Vault name"},
                "resource_type": {
                    "type": "string",
                    "enum": ["document", "table_query", "file"],
                    "description": "Filter by resource type",
                },
            },
            "required": ["vault"],
        },
    ),
    Tool(
        name="akb_publication_snapshot",
        description=(
            "Freeze a table_query publication's current result to S3 and flip its "
            "mode to 'snapshot' (subsequent visits return the cached result). "
            "Identified by `slug` alone — the vault is resolved from the publication. "
            "Returns the updated publication dict."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Publication slug"},
            },
            "required": ["slug"],
        },
    ),
    Tool(
        name="akb_vault_info",
        description="Get detailed vault information: owner, member count, document/table/file/edge counts, last activity.",
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Vault name"},
            },
            "required": ["vault"],
        },
    ),
    Tool(
        name="akb_vault_members",
        description="List all members of a vault with their roles (owner, admin, writer, reader).",
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Vault name"},
            },
            "required": ["vault"],
        },
    ),
    Tool(
        name="akb_grant",
        description="Grant vault access to a user. You must be owner or admin of the vault.",
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Vault name"},
                "user": {"type": "string", "description": "Target username"},
                "role": {
                    "type": "string",
                    "description": "Role to grant",
                    "enum": ["reader", "writer", "admin"],
                },
            },
            "required": ["vault", "user", "role"],
        },
    ),
    Tool(
        name="akb_revoke",
        description="Revoke a user's vault access. You must be owner or admin.",
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Vault name"},
                "user": {"type": "string", "description": "Target username"},
            },
            "required": ["vault", "user"],
        },
    ),
    Tool(
        name="akb_search_users",
        description="Search for users by username, display name, or email. Use this to find users before granting vault access.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (name, email, etc.)"},
                "limit": {"type": "integer", "default": 20},
            },
        },
    ),
    Tool(
        name="akb_whoami",
        description="Get your current profile — username, email, display name, role. Use this to check who you are authenticated as.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="akb_transfer_ownership",
        description="Transfer vault ownership to another user. Only the current owner can do this.",
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Vault name"},
                "new_owner": {"type": "string", "description": "Username of the new owner"},
            },
            "required": ["vault", "new_owner"],
        },
    ),
    Tool(
        name="akb_archive_vault",
        description="Archive a vault (makes it read-only). Only the owner can do this.",
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Vault name"},
            },
            "required": ["vault"],
        },
    ),
    Tool(
        name="akb_delete_vault",
        description=(
            "Permanently delete a vault and ALL its data — documents, chunks, tables, files, edges, Git repo. "
            "This cannot be undone. Owner or admin only."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Vault name to delete"},
            },
            "required": ["vault"],
        },
    ),
    Tool(
        name="akb_create_collection",
        description=(
            "Create an empty collection (folder) inside a vault. Idempotent — "
            "returns {created: false} if the collection already exists. Writer or higher role."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Vault name"},
                "path":  {"type": "string", "description": "Collection path, e.g. 'api-specs' or 'docs/guides'"},
                "summary": {"type": "string", "description": "Optional one-line description"},
            },
            "required": ["vault", "path"],
        },
    ),
    Tool(
        name="akb_delete_collection",
        description=(
            "Delete a collection. If empty, removes the metadata row. If non-empty, requires "
            "recursive=true to cascade delete every document and file under the path. "
            "Cascade emits one git commit for the entire batch. Writer or higher role."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Vault name"},
                "path":  {"type": "string", "description": "Collection path to delete"},
                "recursive": {
                    "type": "boolean",
                    "default": False,
                    "description": "Required when the collection is non-empty.",
                },
            },
            "required": ["vault", "path"],
        },
    ),
    Tool(
        name="akb_set_public",
        description="Set vault public access level. Owner only. 'none'=private, 'reader'=public read, 'writer'=public read+write.",
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Vault name"},
                "level": {"type": "string", "enum": ["none", "reader", "writer"], "description": "Public access level"},
            },
            "required": ["vault", "level"],
        },
    ),
    Tool(
        name="akb_history",
        description=(
            "Get version history of a document — who changed it, when, and why. "
            "Each entry is a Git commit. Use the commit hash with akb_get to read a previous version."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Document URI"},
                "limit": {"type": "integer", "default": 20, "description": "Max entries"},
            },
            "required": ["uri"],
        },
    ),
    Tool(
        name="akb_help",
        description=(
            "Get help on AKB tools and workflows. "
            "Call with no arguments for an overview. "
            "Drill down into categories or specific tools for details and examples. "
            "START HERE if you're new to AKB."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": (
                        "What to get help on. Options: "
                        "categories (quickstart, documents, search, tables, files, access, history, publishing, relations), "
                        "tool names (akb_put, akb_search, etc.), "
                        "or workflow names (link-resources, research, onboarding, data-tracking, vault-skill)"
                    ),
                },
                "vault": {
                    "type": "string",
                    "description": "Vault name. Required for topic='vault-skill' — returns that vault's skill doc body if it exists.",
                },
            },
        },
    ),
]
