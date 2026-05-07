"""AKB MCP Tool Definitions.

All tool schemas for the AKB MCP server.
"""

from mcp.types import Tool


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
                    "description": "Vault template to apply (pre-creates collections with guides). Ignored when external_git is set.",
                    "enum": ["engineering", "qa", "hr", "finance", "management", "issue-tracking", "product"],
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
            "Store a new document in the knowledge base. "
            "The document is saved as a versioned Markdown file with structured metadata. "
            "Automatically chunked and indexed for semantic search."
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
                    "enum": ["note", "report", "decision", "spec", "plan", "session", "task", "reference"],
                    "default": "note",
                },
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags for classification"},
                "domain": {"type": "string", "description": "Domain: engineering, product, ops, legal, etc."},
                "summary": {"type": "string", "description": "Brief summary (auto-generated if omitted)"},
                "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Doc IDs or akb:// URIs this depends on"},
                "related_to": {"type": "array", "items": {"type": "string"}, "description": "Doc IDs or akb:// URIs of related resources"},
            },
            "required": ["vault", "collection", "title", "content"],
        },
    ),
    Tool(
        name="akb_get",
        description=(
            "Retrieve a document by its ID or path. Returns full content with metadata. "
            "Use akb_browse or akb_search first to find the document ID. "
            "Optionally pass a commit hash (from akb_history) to read a previous version."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Vault name"},
                "doc_id": {"type": "string", "description": "Document ID or file path within the vault"},
                "version": {"type": "string", "description": "Git commit hash for a specific version (from akb_history)"},
            },
            "required": ["vault", "doc_id"],
        },
    ),
    Tool(
        name="akb_update",
        description="Update an existing document. Only provide fields you want to change.",
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Vault name"},
                "doc_id": {"type": "string", "description": "Document ID"},
                "content": {"type": "string", "description": "New document body (replaces existing)"},
                "title": {"type": "string", "description": "New title"},
                "status": {"type": "string", "enum": ["draft", "active", "archived", "superseded"]},
                "tags": {"type": "array", "items": {"type": "string"}},
                "summary": {"type": "string"},
                "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Update dependency list (doc IDs or akb:// URIs)"},
                "related_to": {"type": "array", "items": {"type": "string"}, "description": "Update related list (doc IDs or akb:// URIs)"},
                "message": {"type": "string", "description": "Commit message describing the change"},
            },
            "required": ["vault", "doc_id"],
        },
    ),
    Tool(
        name="akb_edit",
        description=(
            "Edit a single document by replacing exact text. Scope is one document (doc_id required). "
            "Use this for precise partial edits. "
            "old_string must be unique within the document (or use replace_all). "
            "If old_string is not found or appears multiple times, the call fails with a clear error. "
            "For find-and-replace across many documents, use akb_grep with replace instead."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Vault name"},
                "doc_id": {"type": "string", "description": "Document ID"},
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
            "required": ["vault", "doc_id", "old_string", "new_string"],
        },
    ),
    Tool(
        name="akb_delete",
        description="Delete a document from the knowledge base. Removes from Git, search index, and knowledge graph.",
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Vault name"},
                "doc_id": {"type": "string", "description": "Document ID"},
            },
            "required": ["vault", "doc_id"],
        },
    ),
    Tool(
        name="akb_browse",
        description=(
            "Browse ALL vault content — documents (by collection), tables, and files. "
            "Without collection: shows top-level collections, tables, and files. "
            "With collection: shows documents and files in that collection. "
            "Use content_type to filter by type."
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
            "(code, URLs, version numbers) prefer akb_grep. Returns doc summaries with "
            "fusion scores; use akb_drill_down or akb_get for full content."
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
                    "enum": ["note", "report", "decision", "spec", "plan", "session", "task", "reference"],
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
            "Returns matching documents with the matched lines and their section paths. "
            "Optionally pass `replace` to find-and-replace across all matching documents."
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
                "vault": {"type": "string", "description": "Vault name"},
                "doc_id": {"type": "string", "description": "Document ID"},
                "section": {"type": "string", "description": "Section path filter (partial match, e.g. 'Background')"},
            },
            "required": ["vault", "doc_id"],
        },
    ),
    Tool(
        name="akb_session_start",
        description="Start an agent work session. Documents created during the session are automatically linked.",
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Vault name"},
                "agent_id": {"type": "string", "description": "Agent identifier"},
                "context": {"type": "string", "description": "What this session is about"},
            },
            "required": ["vault", "agent_id"],
        },
    ),
    Tool(
        name="akb_session_end",
        description="End an agent work session and record a summary.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID from akb_session_start"},
                "summary": {"type": "string", "description": "Summary of what was done"},
            },
            "required": ["session_id"],
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
                "vault": {"type": "string", "description": "Vault name"},
                "doc_id": {"type": "string", "description": "Document ID"},
                "commit": {"type": "string", "description": "Commit hash (from akb_history or akb_activity)"},
            },
            "required": ["vault", "doc_id", "commit"],
        },
    ),
    Tool(
        name="akb_relations",
        description=(
            "Get relations for any resource (document, table, or file). "
            "Shows cross-type connections: doc→table, doc→file, table→file, etc. "
            "Get the resource_uri from akb_browse results."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Vault name"},
                "resource_uri": {"type": "string", "description": "AKB URI (e.g. akb://vault/doc/specs/api.md, akb://vault/table/expenses, akb://vault/file/abc123)"},
                "direction": {"type": "string", "enum": ["incoming", "outgoing", "both"], "default": "both"},
                "type": {"type": "string", "description": "Filter by relation type (depends_on, related_to, implements, references, attached_to)"},
            },
            "required": ["vault", "resource_uri"],
        },
    ),
    Tool(
        name="akb_graph",
        description=(
            "Get a knowledge graph — nodes (documents, tables, files) and edges (relations). "
            "Provide resource_uri to get a subgraph centered on any resource with BFS traversal. "
            "If omitted, returns the full vault graph showing all cross-type connections."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Vault name"},
                "resource_uri": {"type": "string", "description": "Center resource URI (omit for full vault graph)"},
                "depth": {"type": "integer", "default": 2, "minimum": 1, "maximum": 5, "description": "BFS depth"},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200, "description": "Max nodes"},
            },
            "required": ["vault"],
        },
    ),
    Tool(
        name="akb_link",
        description=(
            "Create a relation between any two resources (documents, tables, files). "
            "Uses AKB URIs: akb://{vault}/doc/{path}, akb://{vault}/table/{name}, akb://{vault}/file/{id}. "
            "Relation types: depends_on, related_to, implements, references, attached_to, derived_from. "
            "Example: link a design doc to its data table, or attach a diagram file to a spec."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Vault name"},
                "source": {"type": "string", "description": "Source resource URI (e.g. akb://vault/doc/specs/api.md)"},
                "target": {"type": "string", "description": "Target resource URI (e.g. akb://vault/table/experiments)"},
                "relation": {
                    "type": "string",
                    "description": "Relation type",
                    "enum": ["depends_on", "related_to", "implements", "references", "attached_to", "derived_from"],
                },
            },
            "required": ["vault", "source", "target", "relation"],
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
                "vault": {"type": "string", "description": "Vault name"},
                "source": {"type": "string", "description": "Source resource URI"},
                "target": {"type": "string", "description": "Target resource URI"},
                "relation": {"type": "string", "description": "Specific relation type to remove (omit to remove all)"},
            },
            "required": ["vault", "source", "target"],
        },
    ),
    Tool(
        name="akb_provenance",
        description="Get provenance for a document — who created it, when, which entities were extracted.",
        inputSchema={
            "type": "object",
            "properties": {
                "doc_id": {"type": "string", "description": "Document ID"},
            },
            "required": ["doc_id"],
        },
    ),
    Tool(
        name="akb_create_table",
        description=(
            "Create a structured data table in a vault. "
            "Tables live alongside documents and follow the same permissions. "
            "Define columns with name and type (text, number, boolean, date, json)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string"},
                "name": {"type": "string", "description": "Table name"},
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
                "vault": {"type": "string", "description": "Vault name"},
                "table": {"type": "string", "description": "Table name to delete"},
            },
            "required": ["vault", "table"],
        },
    ),
    Tool(
        name="akb_alter_table",
        description="Modify a table's schema — add, remove, or rename columns via ALTER TABLE DDL. Requires admin role.",
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Vault name"},
                "table": {"type": "string", "description": "Table name"},
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
            "required": ["vault", "table"],
        },
    ),
    Tool(
        name="akb_todo",
        description=(
            "Create a todo for yourself or someone else. "
            "Todos are personal task items — like assigning a ticket. "
            "Use akb_todos to check your pending items."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "What needs to be done"},
                "assignee": {"type": "string", "description": "Username to assign to (omit = yourself)"},
                "vault": {"type": "string", "description": "Related vault (optional)"},
                "note": {"type": "string", "description": "Additional details"},
                "ref_doc": {"type": "string", "description": "Related document ID (optional)"},
                "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"], "default": "normal"},
                "due_date": {"type": "string", "description": "Due date (YYYY-MM-DD)"},
            },
            "required": ["title"],
        },
    ),
    Tool(
        name="akb_todos",
        description=(
            "List todos — yours or someone else's. "
            "Call at session start to see what needs your attention. "
            "Shows open todos by default."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "assignee": {"type": "string", "description": "Username (omit = yourself)"},
                "status": {"type": "string", "enum": ["open", "done", "all"], "default": "open"},
                "vault": {"type": "string", "description": "Filter by vault"},
                "limit": {"type": "integer", "default": 20},
            },
        },
    ),
    Tool(
        name="akb_todo_update",
        description="Update a todo — mark as done, change priority, reassign, or edit.",
        inputSchema={
            "type": "object",
            "properties": {
                "todo_id": {"type": "string", "description": "Todo ID"},
                "status": {"type": "string", "enum": ["open", "done"]},
                "title": {"type": "string"},
                "note": {"type": "string"},
                "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]},
                "assignee": {"type": "string", "description": "Reassign to another user"},
                "due_date": {"type": "string"},
            },
            "required": ["todo_id"],
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
            "Supports expiration, password protection, view count limits, snapshots, and section filtering. "
            "Returns a shareable URL accessible without authentication. "
            "Prefer `public_url_full` (absolute URL) when sharing the link with a user; "
            "fall back to `public_url` (relative path) only if `public_url_full` is null."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Vault name"},
                "resource_type": {
                    "type": "string",
                    "enum": ["document", "table_query", "file"],
                    "default": "document",
                    "description": "Type of resource to share",
                },
                "doc_id": {"type": "string", "description": "Document ID (for resource_type=document)"},
                "file_id": {"type": "string", "description": "File ID (for resource_type=file)"},
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
            "required": ["vault"],
        },
    ),
    Tool(
        name="akb_unpublish",
        description="Remove a public share. Provide either slug or doc_id (deletes all shares for that document).",
        inputSchema={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Vault name"},
                "doc_id": {"type": "string", "description": "Document ID — deletes all publications for this doc"},
                "slug": {"type": "string", "description": "Publication slug — deletes that specific publication"},
            },
            "required": ["vault"],
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
                "publication_id": {"type": "string", "description": "Publication UUID"},
            },
            "required": ["vault", "publication_id"],
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
        name="akb_update_profile",
        description="Update your display name or email.",
        inputSchema={
            "type": "object",
            "properties": {
                "display_name": {"type": "string", "description": "New display name"},
                "email": {"type": "string", "description": "New email address"},
            },
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
                "vault": {"type": "string", "description": "Vault name"},
                "doc_id": {"type": "string", "description": "Document ID"},
                "limit": {"type": "integer", "default": 20, "description": "Max entries"},
            },
            "required": ["vault", "doc_id"],
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
            },
        },
    ),
]
