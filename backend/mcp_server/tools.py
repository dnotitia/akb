"""AKB MCP Tool Definitions.

All tool schemas for the AKB MCP server.

Identifier policy
-----------------
Clients never see internal database IDs (UUIDs, prefixed `d-…` ids,
file UUIDs, table UUIDs). The single canonical handle for every vault
resource is the AKB URI:

  - akb://{vault}/doc/{path/to/file.md}
  - akb://{vault}/table/{name}
  - akb://{vault}/file/{uuid}

Tools that target an existing resource take `uri` and nothing else
(vault is encoded in the URI). Tools that *create* a new resource keep
`vault` + identity bits (title, collection, name) since no URI exists
yet — the returned payload then carries the newly-minted `uri`.

The pair tools (`akb_link`, `akb_unlink`) take `source` + `target`
URIs — they too do not need a separate `vault` arg.

Opaque domain handles that are *not* vault resources keep their own
ID parameter (`session_id`, `todo_id`, `memory_id`, publication
`slug`); these are not URI-addressable.
"""

from mcp.types import Tool

from app.services import template_registry


TOOLS = [
    Tool(
        name="akb_list_vaults",
        description="List all accessible knowledge base vaults.",
        inputSchema={
            "type": "object",
            "properties": {},
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
            "Store a new document. The response carries the canonical `uri` "
            "(akb://{vault}/doc/{path}) — use that to address the document from "
            "every other tool. Automatically chunked and indexed for semantic search."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Target vault name"},
                "collection": {"type": "string", "description": "Collection (directory) path, e.g. 'api-specs' or 'meeting-notes'"},
                "title": {"type": "string", "description": "Document title"},
                "content": {"type": "string", "description": "Document body in Markdown"},
                "type": {
                    "type": "string",
                    "description": "Document type",
                    "enum": ["note", "report", "decision", "spec", "plan", "session", "task", "reference", "skill"],
                    "default": "note",
                },
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags for classification"},
                "domain": {"type": "string", "description": "Domain: engineering, product, ops, legal, etc."},
                "summary": {"type": "string", "description": "Brief summary (auto-generated if omitted)"},
                "depends_on": {"type": "array", "items": {"type": "string"}, "description": "akb:// URIs this depends on"},
                "related_to": {"type": "array", "items": {"type": "string"}, "description": "akb:// URIs of related resources"},
            },
            "required": ["vault", "collection", "title", "content"],
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
                "uri": {"type": "string", "description": "Document URI (akb://{vault}/doc/{path})"},
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
                "status": {"type": "string", "enum": ["draft", "active", "archived", "superseded"]},
                "tags": {"type": "array", "items": {"type": "string"}},
                "summary": {"type": "string"},
                "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Update dependency list (akb:// URIs)"},
                "related_to": {"type": "array", "items": {"type": "string"}, "description": "Update related list (akb:// URIs)"},
                "message": {"type": "string", "description": "Commit message describing the change"},
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
            },
            "required": ["uri", "old_string", "new_string"],
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
            "Browse ALL vault content — documents (by collection), tables, and files. "
            "Without collection: shows top-level collections, tables, and files. "
            "With collection: shows documents and files in that collection. "
            "Each item carries its canonical `uri` — pass that URI to akb_get / akb_update / akb_delete."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Vault name"},
                "collection": {"type": "string", "description": "Collection path to browse into (omit for top-level)"},
                "depth": {
                    "type": "integer",
                    "description": "1=collections only, 2=collections+documents",
                    "default": 1,
                    "minimum": 1,
                    "maximum": 2,
                },
                "content_type": {
                    "type": "string",
                    "enum": ["all", "documents", "tables", "files"],
                    "default": "all",
                    "description": "Filter by content type",
                },
            },
            "required": ["vault"],
        },
    ),
    Tool(
        name="akb_search",
        description=(
            "Search documents with hybrid retrieval — dense vector (semantic) fused with "
            "BM25 sparse (keyword) via Reciprocal Rank Fusion. Handles both natural-language "
            "questions and short keyword queries well. For exact string / regex matches "
            "(code, URLs, version numbers) prefer akb_grep. Returns each hit's `uri`; "
            "use akb_drill_down or akb_get with that URI for full content."
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
            "(grep -l — just the URIs that contain the pattern)."
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
            "Get section-level (L3) content of a document. "
            "Returns all sections with their heading paths, or a specific section if filtered. "
            "Use this to read specific parts without loading the entire document."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Document URI"},
                "section": {"type": "string", "description": "Section path filter (partial match, e.g. 'Background')"},
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
                "type": {"type": "string", "description": "Filter by relation type (depends_on, related_to, implements, references, attached_to)"},
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
                "depth": {"type": "integer", "default": 2, "minimum": 1, "maximum": 5, "description": "BFS depth"},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200, "description": "Max nodes"},
            },
        },
    ),
    Tool(
        name="akb_link",
        description=(
            "Create a relation between any two resources (documents, tables, files). "
            "Source and target are AKB URIs. "
            "Relation types: depends_on, related_to, implements, references, attached_to, derived_from. "
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
                    "enum": ["depends_on", "related_to", "implements", "references", "attached_to", "derived_from"],
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
                "relation": {"type": "string", "description": "Specific relation type to remove (omit to remove all)"},
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
            "canonical `uri` (akb://{vault}/table/{name}). "
            "Tables live alongside documents inside collections and follow the same permissions. "
            "Define columns with name and type (text, number, boolean, date, json). "
            "Optional `collection` (e.g. 'sessions/learnings') groups the table under that "
            "collection so it appears beside the documents and files there in akb_browse; "
            "omit for vault root."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string"},
                "name": {"type": "string", "description": "Table name (unique within the vault)"},
                "collection": {
                    "type": "string",
                    "description": "Collection path (e.g. 'specs' or 'sessions/learnings'). Omit for vault root.",
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
            "required": ["vault", "name", "columns"],
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
                "uri": {"type": "string", "description": "Table URI (akb://{vault}/table/{name})"},
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
                "uri": {"type": "string", "description": "Table URI (akb://{vault}/table/{name})"},
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
        name="akb_remember",
        description=(
            "Store something in your persistent memory. "
            "Memories persist across sessions — use this to remember important context, "
            "decisions, preferences, or learnings for future sessions. "
            "Categories: context (current work), preference (how you like to work), "
            "learning (things you learned), work (completed work), general."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "What to remember"},
                "category": {
                    "type": "string",
                    "description": "Memory category",
                    "enum": ["context", "preference", "learning", "work", "general"],
                    "default": "general",
                },
            },
            "required": ["content"],
        },
    ),
    Tool(
        name="akb_recall",
        description=(
            "Retrieve your persistent memories from previous sessions. "
            "Call this at the start of a session to recall what you were working on. "
            "Filter by category for specific types of memory."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Filter by category (omit for all)",
                    "enum": ["context", "preference", "learning", "work", "general"],
                },
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 50},
            },
        },
    ),
    Tool(
        name="akb_forget",
        description="Delete a specific memory by its ID.",
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "Memory ID to delete"},
            },
            "required": ["memory_id"],
        },
    ),
    Tool(
        name="akb_publish",
        description=(
            "Create a public share URL for a document, table query, or file. "
            "For a document or file, pass the resource `uri`. For a table query, "
            "pass the SQL plus `vault` (queries can span multiple vaults — list them "
            "in query_vault_names). "
            "Supports expiration, password protection, view count limits, snapshots, and section filtering. "
            "Returns a shareable URL accessible without authentication. "
            "Prefer `public_url_full` (absolute URL) when sharing the link with a user; "
            "fall back to `public_url` (relative path) only if `public_url_full` is null."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Resource URI to publish (document or file). Omit for table_query."},
                "resource_type": {
                    "type": "string",
                    "enum": ["document", "table_query", "file"],
                    "default": "document",
                    "description": "Type of resource to share. For document/file, also pass uri. For table_query, pass query_sql + vault.",
                },
                "vault": {"type": "string", "description": "Vault name (required for resource_type=table_query)"},
                "query_sql": {
                    "type": "string",
                    "description": "SELECT SQL with :param placeholders (for resource_type=table_query)",
                },
                "query_vault_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Vaults referenced by the query (defaults to [vault])",
                },
                "query_params": {
                    "type": "object",
                    "description": "Parameter declarations: {name: {type, default, required}}",
                },
                "password": {"type": "string", "description": "Password to protect the share"},
                "max_views": {"type": "integer", "description": "Auto-expire after N views"},
                "expires_in": {
                    "type": "string",
                    "description": "Expiration: '1h', '7d', '30d', or 'never' (default)",
                },
                "title": {"type": "string", "description": "Override display title"},
                "mode": {
                    "type": "string",
                    "enum": ["live", "snapshot"],
                    "default": "live",
                    "description": "live=query each request, snapshot=cache result in S3",
                },
                "section": {
                    "type": "string",
                    "description": "(document) Filter to a specific heading section",
                },
                "allow_embed": {
                    "type": "boolean",
                    "default": True,
                    "description": "Whether the share can be embedded via iframe/oEmbed",
                },
            },
        },
    ),
    Tool(
        name="akb_unpublish",
        description=(
            "Remove a public share. Pass `slug` to delete a single publication, "
            "or `uri` to delete all publications for that resource."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Resource URI — deletes all publications for this resource"},
                "slug": {"type": "string", "description": "Publication slug — deletes that specific publication"},
            },
        },
    ),
    Tool(
        name="akb_publications",
        description="List all publications in a vault (documents, table queries, files).",
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
        description="Create a snapshot of a table_query publication. Saves the current query result to S3 and switches mode to 'snapshot'.",
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Vault name"},
                "slug": {"type": "string", "description": "Publication slug"},
            },
            "required": ["vault", "slug"],
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
                        "categories (quickstart, documents, search, tables, access, memory, sessions, publishing), "
                        "tool names (akb_put, akb_search, etc.), "
                        "or workflow names (link-documents, research, onboarding, data-tracking)"
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
