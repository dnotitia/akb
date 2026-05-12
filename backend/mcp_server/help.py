"""AKB Help System — progressive disclosure documentation for MCP tools.

Call akb_help() for overview, akb_help(topic='...') to drill down.
"""

from __future__ import annotations


HELP = {
    # ── Root ──────────────────────────────────────────────────
    None: """# AKB — Agent Knowledge Base

A team knowledge base where AI agents are first-class citizens.
Three data types live in **vaults**: documents (Markdown/Git), tables (PostgreSQL), and files (S3).
All connected via a unified knowledge graph with AKB URI scheme.

## Quick Start (3 steps)

1. `akb_list_vaults` → see what vaults you have access to
2. `akb_browse(vault="...")` → explore ALL vault content (documents, tables, files)
3. `akb_search(query="...")` → find documents by meaning

## Tool Categories — drill down with `akb_help(topic="...")`

| Topic | Tools | What it does |
|-------|-------|--------------|
| `quickstart` | — | Step-by-step first session guide |
| `documents` | put, get, update, delete, browse, drill_down | Create and manage documents |
| `search` | search, browse, drill_down | Find and read documents |
| `tables` | create_table, sql, alter_table, drop_table | Structured data (real PG tables + SQL) |
| `files` | put_file, get_file, delete_file | Binary files (S3-backed) |
| `access` | grant, revoke, vault_members, vault_info, ... | Permissions and vault management |
| `todos` | todo, todos, todo_update | Personal task assignments |
| `memory` | remember, recall, forget | Persistent memory across sessions |
| `sessions` | session_start, session_end, activity, diff | Track agent work + activity history |
| `publishing` | publish, unpublish, publications, publication_snapshot | Public sharing for docs/tables/files |
| `relations` | link, unlink, relations, graph, provenance | Knowledge graph — cross-type connections |

## AKB URI Scheme (for cross-type linking)

Every resource has a URI: `akb://{vault}/{type}/{id}`
- `akb://eng/doc/specs/api-v2.md` → document
- `akb://eng/table/experiments` → table
- `akb://eng/file/abc123-def456` → file

Use these URIs with `akb_link` and `akb_relations` to connect any resource to any other.

## Workflows — drill down with `akb_help(topic="...")`

| Workflow | Description |
|----------|-------------|
| `link-resources` | Connect documents, tables, and files |
| `research` | Search → read → summarize pattern |
| `onboarding` | Set up a new vault for a project |
| `data-tracking` | Track structured data (expenses, tasks) |

💡 Tip: Use `akb_help(topic="akb_link")` to see any tool's full parameters and examples.""",

    # ── Categories ────────────────────────────────────────────
    "quickstart": """# Quick Start Guide

## Your first AKB session

### Step 1: Discover
```
akb_list_vaults()
→ [{"name": "engineering", "description": "Engineering docs", ...}]
```

### Step 2: Explore
```
akb_browse(vault="engineering")
→ collections: ["api-specs", "decisions", "meeting-notes"]

akb_browse(vault="engineering", collection="decisions")
→ items: [{name: "use-grpc.md", path: "decisions/use-grpc.md", type: "document", ...}]
```

### Step 3: Read
```
akb_get(vault="engineering", doc_id="decisions/use-grpc.md")
→ full document with metadata

akb_drill_down(vault="engineering", doc_id="decisions/use-grpc.md", section="Background")
→ just the Background section
```
`doc_id` accepts a full UUID, a short `d-xxx` id, or a path substring.

### Step 4: Search
```
akb_search(query="authentication flow")
→ ranked results (documents + tables + files) with source_type, source_id, score
```

### Step 5: Write
```
akb_put(
  vault="engineering",
  collection="decisions",
  title="Switch to OAuth2",
  content="## Decision\\n\\nWe're switching...",
  type="decision",
  tags=["auth", "security"],
  related_to=["d-abc123"]  # link to related doc
)
```

💡 Next: `akb_help(topic="documents")` for full CRUD details""",

    "documents": """# Document Tools

## Overview
Documents are Markdown files stored in Git with structured metadata.
Each document has: vault, collection (directory), title, content, type, tags, status.

## Tools

| Tool | Use when... |
|------|-------------|
| `akb_put` | Creating a new document |
| `akb_get` | Reading a document by ID or path |
| `akb_update` | Changing content, title, status, or tags |
| `akb_edit` | Applying a partial edit via exact string replacement (saves tokens) |
| `akb_delete` | Removing a document |
| `akb_browse` | Exploring what exists (tree view) |
| `akb_drill_down` | Reading specific sections of a long document |
| `akb_create_collection` | Creating an empty collection (folder) in a vault |
| `akb_delete_collection` | Deleting a collection (with optional `recursive=true` cascade) |

## Document Types
`note` (default), `report`, `decision`, `spec`, `plan`, `session`, `task`, `reference`

## Document Status
`draft` → `active` → `archived` or `superseded`

## Key Patterns

**Create with relationships:**
```
akb_put(..., related_to=["d-xxx"], depends_on=["d-yyy"])
```

**Partial read (save tokens):**
```
akb_browse(vault="v")           # L1: collection names
akb_browse(vault="v", depth=2)  # L2: + document summaries
akb_drill_down(vault="v", doc_id="d-xxx", section="API")  # L3: one section
```

**Update only what changed:**
```
akb_update(vault="v", doc_id="d-xxx", tags=["urgent"], message="Mark as urgent")
```

**Partial content edit (exact text replacement):**
```
akb_edit(vault="v", doc_id="d-xxx", old_string="old text", new_string="new text")
```

💡 Details: `akb_help(topic="akb_put")`, `akb_help(topic="akb_edit")`, `akb_help(topic="akb_browse")`""",

    "search": """# Search & Discovery

## akb_grep — Exact Text Search & Replace
Find exact strings or regex patterns across documents. Use when you need precision, not meaning.
Add `replace` to find-and-replace across all matching documents in one call.

```
akb_grep(pattern="PostgreSQL 14")                                      # Find exact string
akb_grep(pattern="api/v1/.*users", regex=true, vault="eng")           # Regex search
akb_grep(pattern="TODO", collection="specs")                           # Search in collection
akb_grep(pattern="PostgreSQL 14", vault="eng", replace="PostgreSQL 16") # Find & replace
```

## akb_search — Hybrid Search
Combines **vector similarity** (semantic meaning) with **keyword matching**.

```
akb_search(query="how does authentication work")
akb_search(query="gRPC vs REST", vault="engineering", type="decision")
akb_search(query="invoice", tags=["finance"])
```

**Results include:**
- `score`: relevance (0-1)
- `title`, `summary`: overview
- `matched_section`: the most relevant chunk
- `source_type`: `"document"`, `"table"`, or `"file"` — **dispatch follow-up tool accordingly**
- `source_id`: pass to `akb_get` / `akb_drill_down` for documents,
               `akb_query_table` for tables, `akb_get_file` for files
- `vault`, `path`, `tags`, `doc_type`: metadata

`akb_search` surfaces documents **and** tables **and** files in one
ranked list. Filter by `type="document"|"table"|"file"` to narrow.

## akb_browse — Tree Navigation
When you want to **see everything** rather than search:

```
akb_browse(vault="v")                        # What collections exist?
akb_browse(vault="v", collection="specs")    # What docs are in specs?
akb_browse(vault="v", depth=2)               # Everything at once
```

## akb_drill_down — Section Reader
When a document is long and you only need one part:

```
akb_drill_down(vault="v", doc_id="d-xxx")                    # All sections
akb_drill_down(vault="v", doc_id="d-xxx", section="Setup")   # Just "Setup"
```

## Search → Read Pattern
```
results = akb_search(query="deployment process")
r = results[0]

# Dispatch by source_type — documents, tables, and files share the search
# surface but need different read tools.
if r.source_type == "document":
    doc = akb_get(vault=r.vault, doc_id=r.source_id)
    section = akb_drill_down(vault=r.vault, doc_id=r.source_id, section="Steps")
elif r.source_type == "table":
    rows = akb_query_table(vault=r.vault, table=r.title)
elif r.source_type == "file":
    file = akb_get_file(vault=r.vault, file_id=r.source_id)
```

💡 Tip: search works across ALL vaults you have access to. Use `vault=` to narrow down.""",

    "tables": """# Structured Data Tables

Tables are **real PostgreSQL tables** — full SQL support including
GROUP BY, JOIN, subqueries, window functions, proper type sorting.
Tables appear in `akb_browse` alongside documents and files.

## Tools

| Tool | Description |
|------|-------------|
| `akb_create_table` | Create table (DDL) |
| `akb_browse(content_type="tables")` | List tables in a vault |
| `akb_sql` | Execute any SQL (SELECT/INSERT/UPDATE/DELETE) |
| `akb_alter_table` | Add/remove/rename columns (DDL) |
| `akb_drop_table` | Delete table (DDL) |
| `akb_link` | Connect table to documents or files |

## Column Types
`text`, `number`, `boolean`, `date`, `json`

## Example
```
akb_create_table(vault="finance", name="expenses",
  columns=[
    {"name": "description", "type": "text"},
    {"name": "amount", "type": "number"},
    {"name": "category", "type": "text"},
    {"name": "date", "type": "date"}
  ])

akb_sql(vault="finance",
  sql="INSERT INTO expenses (description, amount, category) VALUES ('서버 비용', 500000, 'infra')")

akb_sql(vault="finance",
  sql="SELECT category, SUM(amount), COUNT(*) FROM expenses GROUP BY category")

akb_sql(vault="finance",
  sql="SELECT * FROM expenses WHERE amount > 100000 ORDER BY date DESC")
```""",

    "access": """# Access Control & Vault Management

## Roles (highest → lowest)
`owner` → `admin` → `writer` → `reader`

| Role | Read | Write | Manage members | Delete vault |
|------|------|-------|----------------|-------------|
| reader | ✓ | | | |
| writer | ✓ | ✓ | | |
| admin | ✓ | ✓ | ✓ | |
| owner | ✓ | ✓ | ✓ | ✓ |

## Tools

```
akb_search_users(query="kim")           # Find a user
akb_grant(vault="v", user="kim", role="writer")  # Grant access
akb_revoke(vault="v", user="kim")       # Remove access
akb_vault_members(vault="v")            # See who has access
akb_vault_info(vault="v")               # Vault stats
akb_transfer_ownership(vault="v", new_owner="kim")
akb_archive_vault(vault="v")            # Make read-only
```

## Create a Vault with Template
```
akb_create_vault(name="my-project", description="...", template="engineering")
```
Templates: `engineering`, `qa`, `hr`, `finance`, `management`, `issue-tracking`, `product`""",

    "memory": """# Persistent Memory

Memories persist across sessions — the agent remembers things between conversations.

## Tools
```
akb_remember(content="The deploy key is in vault 'ops'", category="context")
akb_recall()                          # All memories
akb_recall(category="learning")       # Only learnings
akb_forget(memory_id="mem-xxx")       # Delete one
```

## Categories
| Category | Use for |
|----------|---------|
| `context` | Current work state, what you're doing |
| `preference` | How the user likes to work |
| `learning` | Things you discovered or learned |
| `work` | Summary of completed work |
| `general` | Anything else |

## Best Practices
- Call `akb_recall()` at session start to restore context
- `akb_remember(category="work")` at session end to log what you did
- Store non-obvious learnings: "vault X uses Korean collection names" """,

    "todos": """# Todos — Personal Task Management

Todos are lightweight task items. You can create them for yourself or assign to others.

## Tools
| Tool | Description |
|------|-------------|
| `akb_todo` | Create a todo (for yourself or someone else) |
| `akb_todos` | List todos (open by default) |
| `akb_todo_update` | Mark done, change priority, reassign |

## Examples

**Check your pending work:**
```
akb_todos()
```

**Assign a task to someone:**
```
akb_todo(title="API 스펙 리뷰해줘", assignee="kim", vault="eng", ref_doc="d-xxx", priority="high")
```

**Mark as done:**
```
akb_todo_update(todo_id="t-xxx", status="done")
```

**Reassign:**
```
akb_todo_update(todo_id="t-xxx", assignee="lee")
```

## Session Start Pattern
```
akb_recall()    → memories from last session
akb_todos()     → what needs my attention
akb_activity()  → what others have been doing
```""",

    "sessions": """# Agent Work Sessions

Sessions track what an agent does during a work period.

## Workflow
```
# Start session
s = akb_session_start(vault="eng", agent_id="claude", context="Reviewing API docs")

# ... do work (put, update, search, etc.) ...

# End session
akb_session_end(session_id=s.session_id, summary="Reviewed 5 API docs, updated 2")
```

## Activity History (Git-based)
```
akb_activity(vault="eng")                              # What happened in this vault?
akb_activity(vault="eng", collection="specs")          # Only specs/ changes
akb_activity(vault="eng", author="김영로")              # What did 김영로 do?
akb_activity(vault="eng", since="2026-04-01", limit=5) # Recent 5 since April
```

## Content Diff
```
akb_diff(vault="eng", doc_id="d-xxx", commit="abc123")  # What changed in this commit?
```""",

    "publishing": """# Public Sharing — Documents, Tables, and Files

Create shareable URLs accessible without authentication. Supports
expiration, password protection, view limits, snapshot mode, and
section filters.

## Document share (default)
```
akb_publish(vault="eng", doc_id="d-xxx")
→ {"slug": "abc123", "public_url": "/p/abc123"}
```

## Document share with options
```
akb_publish(
    vault="eng", doc_id="d-xxx",
    expires_in="7d",       # auto-expire
    password="secret",     # bcrypt-hashed
    max_views=100,         # limit
    section="Architecture",# render only this heading section
    title="Custom title"
)
```

## Table query share (canned SQL with URL parameters)
```
akb_publish(
    vault="sales", resource_type="table_query",
    query_sql="SELECT * FROM pipeline WHERE region = :region AND amount >= :min",
    query_params={
        "region": {"type": "text", "default": "ALL"},
        "min": {"type": "number", "default": 0, "required": false}
    }
)
# Visitors call: /p/{slug}?region=KR&min=1000
# Output formats: ?format=json (default), ?format=csv, ?format=html
# Read-only: only SELECT/WITH allowed
```

## File share
```
akb_publish(vault="docs", resource_type="file", file_id="<file_uuid>")
# /p/{slug} returns metadata; /p/{slug}/download → 302 to S3 presigned URL
```

## List + manage shares
```
akb_publications(vault="eng")                            # All publications in vault
akb_publications(vault="eng", resource_type="file")      # Filter by type

akb_unpublish(vault="eng", slug="abc123")          # Remove specific share
akb_unpublish(vault="eng", doc_id="d-xxx")         # Remove all shares for a doc
```

## Snapshot mode (table_query only)
Freeze a query result to S3 — survives backend restarts and reduces
load. Subsequent /p/{slug} returns the cached snapshot.
```
akb_publish(vault="sales", resource_type="table_query",
            query_sql="SELECT ...", mode="snapshot")
# Or convert an existing share to snapshot:
akb_publication_snapshot(vault="sales", publication_id="<uuid>")
```

💡 Shares can be embedded via `<iframe src="/p/{slug}/embed">` or
via oEmbed at `/api/v1/oembed?url=/p/{slug}`. Disable embedding with
`allow_embed=false`.""",

    "relations": """# Knowledge Graph — Cross-Type Relations

## URI Scheme
Every resource has an AKB URI: `akb://{vault}/{type}/{identifier}`
- Document: `akb://eng/doc/specs/api-v2.md`
- Table: `akb://eng/table/experiments`
- File: `akb://eng/file/abc123`

Browse results include the `uri` field for each resource.

## Relation Types
- `depends_on` — prerequisite dependency
- `related_to` — bidirectional association
- `implements` — implements a spec/decision
- `references` — references data in another resource
- `attached_to` — file attachment (e.g. diagram → doc)
- `derived_from` — data lineage

## Creating Links

**Explicit (any resource type):**
```
akb_link(vault="eng",
  source="akb://eng/doc/specs/api.md",
  target="akb://eng/table/api-endpoints",
  relation="references")

akb_link(vault="eng",
  source="akb://eng/file/abc123",
  target="akb://eng/doc/specs/api.md",
  relation="attached_to")
```

**When creating a document (doc→doc only):**
```
akb_put(..., depends_on=["d-spec1"], related_to=["d-note2"])
```

**Via markdown links in content:**
```
See the [experiment results](akb://eng/table/experiments) for details.
```

## Removing Links
```
akb_unlink(vault="eng",
  source="akb://eng/doc/specs/api.md",
  target="akb://eng/table/api-endpoints",
  relation="references")

akb_unlink(vault="eng",
  source="akb://eng/doc/specs/api.md",
  target="akb://eng/table/api-endpoints")  # removes ALL relations between them
```

## Querying Relations
```
akb_relations(vault="eng", resource_uri="akb://eng/doc/specs/api.md")
akb_relations(vault="eng", resource_uri="akb://eng/table/experiments", direction="incoming")
```

## Graph View
```
akb_graph(vault="eng")                           # Full vault graph (all types)
akb_graph(vault="eng", resource_uri="akb://eng/doc/specs/api.md", depth=2)
```

## Provenance
```
akb_provenance(doc_id="d-xxx")
→ who created it, when, all relations (including cross-type)
```""",

    # ── Workflows ─────────────────────────────────────────────
    "link-documents": """# Workflow: Linking Documents (legacy — see link-resources)
Use `akb_help(topic="link-resources")` for the updated cross-type linking guide.""",

    "link-resources": """# Workflow: Linking Resources (Documents, Tables, Files)

## Goal: Build a connected knowledge graph across all data types

### Step 1: Browse to find resources and their URIs
```
akb_browse(vault="eng")
→ collections, tables, files — each with a `uri` field
```

### Step 2: Create explicit links with akb_link
```
# Link a design doc to the experiment results table
akb_link(vault="eng",
  source="akb://eng/doc/specs/experiment-design.md",
  target="akb://eng/table/experiment-results",
  relation="references")

# Attach a diagram file to a spec
akb_link(vault="eng",
  source="akb://eng/file/arch-diagram-abc123",
  target="akb://eng/doc/specs/architecture.md",
  relation="attached_to")

# Mark a report as derived from a data table
akb_link(vault="eng",
  source="akb://eng/doc/reports/q1-summary.md",
  target="akb://eng/table/quarterly-metrics",
  relation="derived_from")
```

### Step 3: Create document with doc→doc links
```
akb_put(vault="eng", collection="decisions", title="API Redesign",
  content="Based on [experiment results](akb://eng/table/experiments)...",
  depends_on=["d-spec1"], related_to=["d-review"])
```

### Step 4: Verify the graph
```
akb_relations(vault="eng", resource_uri="akb://eng/doc/specs/experiment-design.md")
akb_graph(vault="eng", resource_uri="akb://eng/doc/specs/experiment-design.md", depth=2)
```

### Step 5: Remove a link if needed
```
akb_unlink(vault="eng",
  source="akb://eng/doc/specs/experiment-design.md",
  target="akb://eng/table/experiment-results",
  relation="references")
```

💡 When a resource is deleted, its edges are automatically cleaned up.""",

    "research": """# Workflow: Research a Topic

## Goal: Find everything about X, summarize, create a new document

### Step 1: Broad search
```
akb_search(query="authentication")
```

### Step 2: Read the top results
```
akb_get(vault="eng", doc_id="d-top1")
akb_drill_down(vault="eng", doc_id="d-top2", section="Implementation")
```

### Step 3: Check related documents
```
akb_relations(vault="eng", resource_uri="akb://eng/doc/specs/top1.md")
# Follow the links for more context
```

### Step 4: Write a summary
```
akb_put(vault="eng", collection="reports",
  title="Authentication System Overview",
  type="report",
  content="## Summary\\n\\nBased on ...",
  related_to=["d-top1", "d-top2"],
  tags=["auth", "research"])
```""",

    "onboarding": """# Workflow: Set Up a New Project Vault

### Step 1: Create vault with template
```
akb_create_vault(name="my-project", description="Project X knowledge base", template="engineering")
```
Templates pre-create useful collections (specs, decisions, guides, etc.)

### Step 2: Browse the template structure
```
akb_browse(vault="my-project", depth=2)
```

### Step 3: Invite team members
```
akb_search_users(query="kim")
akb_grant(vault="my-project", user="kim-dev", role="writer")
akb_grant(vault="my-project", user="pm-lee", role="reader")
```

### Step 4: Add initial documents
```
akb_put(vault="my-project", collection="decisions",
  title="Tech Stack Decision", type="decision", ...)
```

### Step 5: Set up data tracking (optional)
```
akb_create_table(vault="my-project", name="tasks",
  columns=[{"name":"task","type":"text"}, {"name":"status","type":"text"}, {"name":"assignee","type":"text"}])
```""",

    "data-tracking": """# Workflow: Track Structured Data

## Use tables for data that doesn't fit in documents

### Example: Sprint Task Board
```
akb_create_table(vault="v", name="tasks",
  columns=[
    {"name": "task", "type": "text"},
    {"name": "assignee", "type": "text"},
    {"name": "status", "type": "text"},
    {"name": "priority", "type": "number"},
    {"name": "due_date", "type": "date"}
  ])

akb_sql(vault="v",
  sql="INSERT INTO tasks (task, assignee, status, priority, due_date) VALUES ('Implement login', 'kim', 'in_progress', 1, '2026-04-10')")

# Who's overdue?
akb_sql(vault="v",
  sql="SELECT * FROM tasks WHERE status='in_progress' AND due_date < '2026-04-03'")

# Summary stats
akb_sql(vault="v",
  sql="SELECT status, COUNT(*) FROM tasks GROUP BY status")
```""",

    # ── Individual Tools ──────────────────────────────────────
    "akb_help": """# akb_help

Get help on AKB tools and workflows.

```
akb_help()                          # Overview + all categories
akb_help(topic="quickstart")        # Step-by-step first session
akb_help(topic="documents")         # Document CRUD tools
akb_help(topic="akb_put")           # Specific tool details
akb_help(topic="link-resources")    # Workflow guide
```""",

    "akb_put": """# akb_put — Store a Document

## Parameters
| Param | Required | Description |
|-------|----------|-------------|
| vault | ✓ | Target vault name |
| collection | ✓ | Directory path (e.g. "api-specs", "meeting-notes") |
| title | ✓ | Document title |
| content | ✓ | Markdown body |
| type | | note, report, decision, spec, plan, session, task, reference |
| tags | | ["auth", "api"] |
| domain | | engineering, product, ops, legal, etc. |
| summary | | Brief summary (auto-generated if omitted) |
| depends_on | | ["d-xxx"] — IDs of prerequisite documents |
| related_to | | ["d-yyy"] — IDs of related documents |

## Examples

**Simple note:**
```
akb_put(vault="eng", collection="notes", title="Meeting Notes 2026-04-03",
  content="## Attendees\\n- Kim, Lee\\n\\n## Discussion\\n...")
```

**Decision record with links:**
```
akb_put(vault="eng", collection="decisions", title="Adopt gRPC",
  type="decision", tags=["grpc", "api"],
  content="## Context\\n...\\n## Decision\\n...\\n## Consequences\\n...",
  depends_on=["d-api-spec"],
  related_to=["d-rest-analysis"])
```

## Returns
```json
{"doc_id": "d-abc123", "path": "decisions/adopt-grpc.md", "chunks_indexed": 5}
```""",

    "akb_get": """# akb_get — Retrieve a Document

## Parameters
| Param | Required | Description |
|-------|----------|-------------|
| vault | ✓ | Vault name |
| doc_id | ✓ | Document ID (`d-xxx`) or file path (`decisions/my-doc.md`) |

## Returns
Full document: title, content, metadata (type, tags, status, created_by, dates), relations.

## Examples
```
akb_get(vault="eng", doc_id="d-abc123")       # by ID
akb_get(vault="eng", doc_id="decisions/adopt-grpc.md")  # by path
```

💡 For large documents, use `akb_drill_down` to read specific sections.""",

    "akb_update": """# akb_update — Update a Document

Only send fields you want to change. Unspecified fields remain unchanged.

## Parameters
| Param | Required | Description |
|-------|----------|-------------|
| vault | ✓ | Vault name |
| doc_id | ✓ | Document ID |
| content | | New Markdown body (replaces existing) |
| title | | New title |
| status | | draft, active, archived, superseded |
| tags | | New tag list (replaces existing) |
| summary | | New summary |
| depends_on | | Update dependency list (doc IDs or akb:// URIs) |
| related_to | | Update related list (doc IDs or akb:// URIs) |
| message | | Git commit message |

## Examples
```
akb_update(vault="eng", doc_id="d-xxx", tags=["urgent", "reviewed"],
  message="Mark as urgent and reviewed")

akb_update(vault="eng", doc_id="d-xxx", status="archived",
  message="Superseded by d-yyy")
```""",

    "akb_edit": """# akb_edit — Edit a Document by Exact Text Replacement

Apply a partial edit to a document's body by replacing exact text.
Much more efficient than akb_update(content=...) for small changes to large documents,
and much more reliable than line-based patching because LLMs don't have to count lines.

The edit is applied to the **Markdown body only** — frontmatter is unchanged.
Use akb_update for metadata changes (title, tags, status, etc.).

## Parameters
| Param | Required | Description |
|-------|----------|-------------|
| vault | ✓ | Vault name |
| doc_id | ✓ | Document ID |
| old_string | ✓ | Exact text to find (must be unique unless replace_all=true) |
| new_string | ✓ | Replacement text (can be empty to delete) |
| replace_all | | If true, replaces every occurrence (default: false) |
| message | | Git commit message |

## Uniqueness Rule
By default, `old_string` must match **exactly one** place in the document body.
If it appears multiple times, include more surrounding context to make it unique,
or set `replace_all=true` to replace every occurrence.

## Workflow
1. `akb_get(vault, doc_id)` → read current content
2. Pick a distinctive piece of text you want to change
3. `akb_edit(vault, doc_id, old_string="...", new_string="...")` → apply

## Error Handling
Errors return `error: "edit_failed"` with a message explaining:
- `old_string cannot be empty`
- `old_string not found` — fetch fresh content and retry
- `old_string appears N times` — add context or set replace_all=true

## Examples
```
# Fix a typo
akb_edit(vault="eng", doc_id="d-abc123",
  old_string="teh old typo",
  new_string="the old typo",
  message="Fix typo")

# Replace a whole paragraph (include enough context to be unique)
akb_edit(vault="eng", doc_id="d-abc123",
  old_string="## Section A\\n\\nOriginal content of section A.",
  new_string="## Section A\\n\\nUpdated content with more details.",
  message="Rewrite section A")

# Delete a line (empty new_string)
akb_edit(vault="eng", doc_id="d-abc123",
  old_string="\\nTODO: remove this line\\n",
  new_string="\\n")

# Replace every occurrence of a term
akb_edit(vault="eng", doc_id="d-abc123",
  old_string="PostgreSQL 14",
  new_string="PostgreSQL 16",
  replace_all=true,
  message="Upgrade Postgres version")
```

💡 Tip: for whole-document rewrites use akb_update; for find-and-replace across many documents use akb_grep with `replace`.""",

    "akb_browse": """# akb_browse — Browse ALL Vault Content

Shows documents (by collection), tables, and files in one unified view.
Each item includes its `uri` for use with akb_link, akb_relations, etc.

## Parameters
| Param | Required | Description |
|-------|----------|-------------|
| vault | ✓ | Vault name |
| collection | | Collection path (omit for top-level) |
| depth | | 1 = collections only, 2 = collections + documents |
| content_type | | all, documents, tables, files (default: all) |

## Examples
```
akb_browse(vault="eng")                         # Everything: collections, tables, files
akb_browse(vault="eng", content_type="tables")  # Only tables
akb_browse(vault="eng", collection="specs")     # Documents + files in "specs"
akb_browse(vault="eng", depth=2)                # Collections expanded + tables + files
```

💡 Use content_type to filter when you only need one data type.""",

    "akb_search": """# akb_search — Hybrid Search

Combines vector similarity (meaning) with keyword matching.

## Parameters
| Param | Required | Description |
|-------|----------|-------------|
| query | ✓ | Natural language search query |
| vault | | Limit to specific vault |
| collection | | Limit to specific collection |
| type | | Filter: note, report, decision, spec, plan, session, task, reference |
| tags | | Filter by tags |
| limit | | Max results (default 10, max 50) |

## Examples
```
akb_search(query="deployment process")
akb_search(query="인증 흐름", vault="eng", type="spec")
akb_search(query="budget", tags=["finance"], limit=5)
```

## Result Fields
- `doc_id`: use with akb_get or akb_drill_down
- `title`, `summary`: overview
- `score`: relevance (0.0-1.0)
- `matched_section`: the most relevant chunk
- `vault`, `collection`, `type`, `tags`: metadata""",

    "akb_grep": """# akb_grep — Exact Text / Regex Search & Replace

Find exact strings or regex patterns across document content.
Unlike akb_search (semantic), this finds **exact matches** — use it for
specific terms, URLs, code snippets, version numbers, error codes, etc.

Optionally pass `replace` to find-and-replace across all matching documents.

## Parameters
| Param | Required | Description |
|-------|----------|-------------|
| pattern | ✓ | Text or regex to search for |
| vault | | Limit to a specific vault (required for replace) |
| collection | | Limit to a specific collection |
| regex | | Treat pattern as regex (default: false) |
| case_sensitive | | Case-sensitive match (default: false) |
| replace | | Replacement string — triggers find-and-replace mode |
| limit | | Max documents to return (default 20) |

## When to use akb_grep vs akb_search
| Need | Tool |
|------|------|
| Find docs about "authentication" (concept) | `akb_search` |
| Find docs containing "JWT_SECRET" (exact string) | `akb_grep` |
| Find all references to a URL or API path | `akb_grep` |
| Find docs related to a topic | `akb_search` |
| Find docs with a specific version number | `akb_grep` |

## Search Examples
```
akb_grep(pattern="PostgreSQL")
akb_grep(pattern="api/v1/users", vault="eng")
akb_grep(pattern="TODO|FIXME", regex=true)
akb_grep(pattern="Bearer", case_sensitive=true)
```

## Replace Examples
```
# Simple text replace across a vault
akb_grep(pattern="PostgreSQL 14", vault="eng", replace="PostgreSQL 16")

# Regex replace with capture groups
akb_grep(pattern="v(\\d+)\\.1", vault="eng", regex=true, replace="v\\1.2")
```

**Tip:** Run grep WITHOUT replace first to preview matches, then add replace.
Each replaced document gets its own git commit and is re-indexed for search.

## Result Structure
Each result includes `doc_id`, `vault`, `path`, `title`, and `matches` — a list of
`{section, text}` showing the section path and matched line.
When replace is used, response also includes `replaced_docs` count and `replacements` list.""",

    "akb_drill_down": """# akb_drill_down — Section-Level Reader

Read specific sections of a document without loading everything.

## Parameters
| Param | Required | Description |
|-------|----------|-------------|
| vault | ✓ | Vault name |
| doc_id | ✓ | Document ID |
| section | | Section name filter (partial match) |

## Examples
```
akb_drill_down(vault="eng", doc_id="d-xxx")                    # All section headings
akb_drill_down(vault="eng", doc_id="d-xxx", section="Setup")   # Just "Setup" section
akb_drill_down(vault="eng", doc_id="d-xxx", section="API")     # Sections containing "API"
```

💡 Token-efficient: read summaries via browse, then drill into the section you need.""",


    "akb_create_table": """# akb_create_table — Create a Table

## Parameters
| Param | Required | Description |
|-------|----------|-------------|
| vault | ✓ | Vault name |
| name | ✓ | Table name |
| description | | What this table is for |
| columns | ✓ | Column definitions |

## Column Types
`text`, `number`, `boolean`, `date`, `json`

## Example
```
akb_create_table(vault="finance", name="invoices",
  description="Client invoice tracking",
  columns=[
    {"name": "client", "type": "text"},
    {"name": "amount", "type": "number"},
    {"name": "status", "type": "text"},
    {"name": "due_date", "type": "date"},
    {"name": "metadata", "type": "json"}
  ])
```""",

    "akb_list_vaults": """# akb_list_vaults — List Accessible Vaults

No parameters. Returns all vaults you have access to.

```
akb_list_vaults()
→ {"vaults": [{"name": "eng", "description": "...", "role": "writer"}, ...]}
```""",

    "akb_create_vault": """# akb_create_vault — Create a Vault

## Parameters
| Param | Required | Description |
|-------|----------|-------------|
| name | ✓ | Lowercase, hyphens allowed |
| description | | What this vault is for |
| template | | Pre-populate with collections |

## Templates
`engineering`, `qa`, `hr`, `finance`, `management`, `issue-tracking`, `product`

## Example
```
akb_create_vault(name="project-x", description="Project X docs", template="engineering")
```""",

    "akb_create_collection": """# akb_create_collection — Create an Empty Collection

Creates a collection (folder) in a vault. Idempotent — returns
`{created: false}` if it already exists. No git side effect (empty
collections don't materialize as directories).

## Parameters
| Param | Required | Description |
|-------|----------|-------------|
| vault | ✓ | Vault name |
| path | ✓ | Collection path, e.g. 'api-specs', 'docs/guides' |
| summary | | Optional one-line description |

## Example
```
akb_create_collection(vault="eng", path="api-specs", summary="Public API contracts")
```

Requires writer role.""",

    "akb_delete_collection": """# akb_delete_collection — Delete a Collection

The `path` is treated as a **prefix**, not a single row. Deleting `P`
covers the row at `P` (if any) plus every sub-collection, document,
and file under `P/`.

- Empty mode (default): succeeds only if the row at `P` exists and
  nothing else lives under the prefix. Any sub-collection, document,
  or file rejects with `not_empty` and the counts.
- `recursive=true`: cascade-delete the row at `P` (if any), every
  sub-collection row beneath it, all documents (one git commit), and
  all files (s3 outbox).
- A path with no row and no descendants returns a NotFound error.

## Parameters
| Param | Required | Description |
|-------|----------|-------------|
| vault | ✓ | Vault name |
| path | ✓ | Collection path (prefix) |
| recursive | | Default `false`. Set `true` to cascade sub-collections + docs + files. |

## Examples
```
akb_delete_collection(vault="eng", path="old-specs")
akb_delete_collection(vault="eng", path="legacy", recursive=True)
```

Requires writer role.""",

    "akb_delete": """# akb_delete — Delete a Document

⚠️ Permanent. Removes from Git, search index, and knowledge graph edges.

## Parameters
| Param | Required | Description |
|-------|----------|-------------|
| vault | ✓ | Vault name |
| doc_id | ✓ | Document ID or path |

## Example
```
akb_delete(vault="eng", doc_id="d-xxx")
```

Requires writer role. All edges referencing this document are automatically cleaned up.""",

    "akb_relations": """# akb_relations — Resource Relations (Cross-Type)

## Parameters
| Param | Required | Description |
|-------|----------|-------------|
| vault | ✓ | Vault name |
| resource_uri | ✓ | AKB URI (from akb_browse results) |
| direction | | incoming, outgoing, both (default) |
| type | | depends_on, related_to, implements, references, attached_to, derived_from |

## Examples
```
akb_relations(vault="eng", resource_uri="akb://eng/doc/specs/api.md")
akb_relations(vault="eng", resource_uri="akb://eng/table/experiments", direction="incoming")
```""",

    "akb_graph": """# akb_graph — Knowledge Graph (Cross-Type)

Shows nodes (documents, tables, files) and edges (all relation types).

## Parameters
| Param | Required | Description |
|-------|----------|-------------|
| vault | ✓ | Vault name |
| resource_uri | | Center node URI (omit for full vault graph) |
| depth | | BFS traversal depth (1-5, default 2) |
| limit | | Max nodes (1-200, default 50) |

## Examples
```
akb_graph(vault="eng")                                                    # Full vault graph
akb_graph(vault="eng", resource_uri="akb://eng/table/experiments", depth=2)  # From a table
akb_graph(vault="eng", resource_uri="akb://eng/doc/specs/api.md", depth=3)  # 3-hop from doc
```""",

    "akb_provenance": """# akb_provenance — Document Provenance

Shows who created a document, when, and all its relations (including cross-type).

## Parameters
| Param | Required | Description |
|-------|----------|-------------|
| doc_id | ✓ | Document ID |

## Example
```
akb_provenance(doc_id="d-xxx")
→ {title, path, vault, uri, created_by, created_at, updated_at, relations: [...]}
```""",

    "akb_history": """# akb_history — Document Version History

```
akb_history(vault="eng", doc_id="d-xxx")
→ [{"hash": "abc123def456", "date": "2026-04-03T12:00:00", "author": "admin", "message": "Update specs"}]

akb_get(vault="eng", doc_id="d-xxx", version="abc123def456")
→ content at that specific version
```

Each document change creates a Git commit. Use akb_history to see all versions, then akb_get with version= to read any past version.""",

    "akb_session_start": """# akb_session_start

```
akb_session_start(vault="eng", agent_id="claude", context="Reviewing API docs")
→ {"session_id": "s-xxx"}
```
Documents created during the session are auto-linked.""",

    "akb_session_end": """# akb_session_end

```
akb_session_end(session_id="s-xxx", summary="Reviewed 5 docs, updated 2")
```""",

    "akb_activity": """# akb_activity — Vault Activity History (Git-based)

Shows who changed what, when, and why. Each entry is a Git commit.

```
akb_activity(vault="eng")                              # All activity
akb_activity(vault="eng", collection="specs")          # Only specs/ changes
akb_activity(vault="eng", author="김영로")              # By author
akb_activity(vault="eng", since="2026-04-01", limit=5) # Since date
```

Each entry includes:
- hash: commit hash (use with akb_diff)
- subject: commit message
- author/agent: who made the change
- action: create/update/delete
- summary: change description
- files: [{path, change: added/modified/deleted}]

Use `akb_diff(vault, doc_id, commit=hash)` to see the actual content diff.""",

    "akb_diff": """# akb_diff — Document Content Diff

Shows what was added/removed in a specific commit.

```
akb_diff(vault="eng", doc_id="d-xxx", commit="abc123def456")
```

Returns:
- file: document path
- type: added/modified/deleted
- diff: unified diff (+ for additions, - for removals)

Find commit hashes via:
- akb_history(vault, doc_id) → per-document versions
- akb_activity(vault) → vault-wide activity""",

    "akb_remember": """# akb_remember — Store Memory

```
akb_remember(content="Deploy uses vault 'ops', collection 'runbooks'", category="context")
```
Categories: context, preference, learning, work, general""",

    "akb_recall": """# akb_recall — Retrieve Memories

```
akb_recall()                       # All memories
akb_recall(category="learning")   # Only learnings
akb_recall(limit=5)               # Last 5
```""",

    "akb_forget": """# akb_forget — Delete a Memory

```
akb_forget(memory_id="mem-xxx")
```

Get memory IDs from `akb_recall()` results.""",

    "akb_todo": """# akb_todo — Create a Todo

## Parameters
| Param | Required | Description |
|-------|----------|-------------|
| title | ✓ | What needs to be done |
| assignee | | Username (omit = yourself) |
| vault | | Related vault |
| note | | Additional details |
| ref_doc | | Related document ID |
| priority | | low, normal, high, urgent |
| due_date | | YYYY-MM-DD |

## Examples
```
akb_todo(title="Fix login bug")
akb_todo(title="Review PR #45", assignee="kim", priority="high", due_date="2026-04-05")
akb_todo(title="Update API docs", vault="eng", ref_doc="d-xxx")
```""",

    "akb_todos": """# akb_todos — List Todos

```
akb_todos()                          # My open todos
akb_todos(status="all")              # Including done
akb_todos(assignee="kim")            # Kim's todos
akb_todos(vault="eng")               # Only eng vault
```
Sorted by priority (urgent first), then by creation date.""",

    "akb_todo_update": """# akb_todo_update — Update a Todo

```
akb_todo_update(todo_id="t-xxx", status="done")         # Mark done
akb_todo_update(todo_id="t-xxx", priority="urgent")     # Escalate
akb_todo_update(todo_id="t-xxx", assignee="lee")        # Reassign
akb_todo_update(todo_id="t-xxx", due_date="2026-04-10") # Set deadline
```""",

    "akb_publish": """# akb_publish — Create a Public Share

Create a shareable URL accessible without authentication. Supports
documents, table queries, and files.

## Document
```
akb_publish(vault="v", doc_id="d-xxx")
→ {"slug": "abc123", "public_url": "/p/abc123", "publication_id": "..."}

# With options
akb_publish(vault="v", doc_id="d-xxx",
            expires_in="7d", password="secret",
            max_views=100, section="Architecture")
```

## Table query (canned SQL with URL parameters)
```
akb_publish(vault="sales", resource_type="table_query",
            query_sql="SELECT * FROM pipeline WHERE region = :region",
            query_params={"region": {"type": "text", "default": "ALL"}})
# /p/{slug}?region=KR  → JSON  (or ?format=csv|html)
```

## File
```
akb_publish(vault="docs", resource_type="file", file_id="<uuid>")
# /p/{slug} returns metadata; /p/{slug}/download → 302 to S3
```

## Options
| Field | Description |
|-------|-------------|
| `expires_in` | '1h', '7d', '30d', or 'never' (default) |
| `password` | bcrypt-hashed; visitors must POST /p/{slug}/auth |
| `max_views` | auto-expire after N views |
| `title` | display title override |
| `mode` | 'live' (default) or 'snapshot' (table_query only) |
| `section` | (document) filter to a heading section |
| `allow_embed` | true (default) — set false to block iframe/oEmbed |""",

    "akb_unpublish": """# akb_unpublish — Delete a Public Share

```
akb_unpublish(vault="v", slug="abc123")        # Remove specific share
akb_unpublish(vault="v", doc_id="d-xxx")       # Remove all shares for a document
```

The shareable URL stops working immediately.""",

    "akb_publications": """# akb_publications — List Publications

```
akb_publications(vault="v")                          # All publications in a vault
akb_publications(vault="v", resource_type="document") # Filter by type
akb_publications(vault="v", resource_type="file")
akb_publications(vault="v", resource_type="table_query")
```

Returns each share's id, slug, resource_type, title, view_count,
max_views, expires_at, mode, password_protected.""",

    "akb_publication_snapshot": """# akb_publication_snapshot — Freeze a Table Query Publication

Execute a table_query share's SQL once and store the result in S3.
Subsequent /p/{slug} returns the cached snapshot — survives backend
restarts and reduces DB load.

```
akb_publication_snapshot(vault="sales", publication_id="<uuid>")
→ {"snapshot_s3_key": "snapshots/<uuid>.json",
   "snapshot_at": "2026-04-11T...",
   "rows": 123}
```

Only applies to resource_type='table_query'.""",

    "akb_grant": """# akb_grant — Grant Vault Access

```
akb_grant(vault="v", user="kim", role="writer")
```
Roles: reader, writer, admin. Must be owner or admin.""",

    "akb_revoke": """# akb_revoke — Remove Vault Access

```
akb_revoke(vault="v", user="kim")
```

Requires owner or admin role. Cannot revoke the owner.""",

    "akb_vault_info": """# akb_vault_info — Vault Statistics

```
akb_vault_info(vault="v")
→ {name, description, owner, member_count, document_count, table_count, file_count, edge_count, last_activity, created_at}
```""",

    "akb_vault_members": """# akb_vault_members — List Members

```
akb_vault_members(vault="v")
→ [{username, display_name, role, granted_at}, ...]
```

Roles: owner, admin, writer, reader.""",

    "akb_search_users": """# akb_search_users — Find Users

```
akb_search_users(query="kim")
akb_search_users(query="kim", limit=5)
```

Search by username, display name, or email. Use before `akb_grant`.""",

    "akb_transfer_ownership": """# akb_transfer_ownership — Transfer Vault Ownership

```
akb_transfer_ownership(vault="v", new_owner="kim")
```

⚠️ Only the current owner can do this. You become admin after transfer.""",

    "akb_archive_vault": """# akb_archive_vault — Archive Vault (Read-Only)

```
akb_archive_vault(vault="v")
```

⚠️ Makes vault read-only. No new documents, table writes, or file uploads. Owner only.""",

    "akb_set_public": """# akb_set_public — Set Vault Public Access

```
akb_set_public(vault="v", level="reader")
```

Levels:
- `none` — private (default, login required)
- `reader` — anyone can read without login
- `writer` — anyone can read and write without login

Owner only.""",

    "akb_whoami": """# akb_whoami — Check Your Identity

```
akb_whoami()
```

Returns your current profile: username, display name, email, admin status, account creation date.
Use this to verify who you are authenticated as.""",

    "akb_update_profile": """# akb_update_profile — Update Your Profile

```
akb_update_profile(display_name="Kim Younro")
akb_update_profile(email="kim@example.com")
```

Updates display name and/or email for the authenticated user.""",

    "akb_delete_vault": """# akb_delete_vault — Delete Vault

⚠️ **Permanent and irreversible.** Deletes ALL data: documents, tables, files, edges, Git history.

```
akb_delete_vault(vault="v", confirm="v")
```

The `confirm` parameter must match the vault name. Owner only.""",

    "akb_alter_table": """# akb_alter_table — Modify Table Schema

## Parameters
| Param | Required | Description |
|-------|----------|-------------|
| vault | ✓ | Vault name |
| table | ✓ | Table name |
| add_columns | | Columns to add: [{"name": "col", "type": "text"}] |
| drop_columns | | Column names to remove: ["old_col"] |
| rename_columns | | Rename map: {"old_name": "new_name"} |

## Example
```
akb_alter_table(vault="eng", table="tasks",
  add_columns=[{"name": "priority", "type": "number"}],
  drop_columns=["old_field"],
  rename_columns={"desc": "description"})
```

Requires admin role.""",

    "akb_drop_table": """# akb_drop_table — Delete a Table

⚠️ **Permanent.** Deletes the table, all rows, and all edges referencing it.

```
akb_drop_table(vault="eng", table="old-experiments")
```

Requires admin role.""",

    "akb_get_file": """# akb_get_file — Download a File

Downloads from S3 to a local path. Handled by akb-mcp stdio proxy.

## Parameters
| Param | Required | Description |
|-------|----------|-------------|
| vault | ✓ | Vault name |
| file_id | ✓ | File ID (from akb_browse) |
| save_to | ✓ | Local directory or file path |

## Example
```
akb_get_file(vault="eng", file_id="abc123", save_to="/tmp/downloads/")
→ {"name": "diagram.png", "save_to": "/tmp/downloads/diagram.png", "size_bytes": 45000}
```""",

    "akb_delete_file": """# akb_delete_file — Delete a File

Removes from S3, database, and cleans up all edges referencing the file.

```
akb_delete_file(vault="eng", file_id="abc123")
```

Requires writer role.""",


    "akb_sql": """# akb_sql — Execute SQL on Vault Tables

Tables are real PostgreSQL tables. Use standard SQL.

```
akb_sql(vault="sales", sql="SELECT * FROM pipeline WHERE probability >= 60")

akb_sql(vault="sales", sql="SELECT stage, COUNT(*), SUM(amount) FROM pipeline GROUP BY stage")

akb_sql(vault="sales", sql="INSERT INTO pipeline (deal_name, customer, amount) VALUES ('New Deal', 'ACME', 1000000)")

akb_sql(vault="sales", sql="UPDATE pipeline SET stage='closed-won' WHERE deal_name='New Deal'")

akb_sql(vault="sales", sql="DELETE FROM pipeline WHERE deal_name='New Deal'")
```

Cross-vault (use vault prefix):
```
akb_sql(vaults=["sales","external-projects"],
  sql="SELECT * FROM sales__pipeline p JOIN sales__partners c ON ...")
```

Permissions: SELECT=reader, INSERT/UPDATE/DELETE=writer""",

    "files": """# File Storage (S3-backed)

Binary files (images, PDFs, exports) stored in S3 with metadata in PostgreSQL.
Files appear in `akb_browse` alongside documents and tables.

File tools (`akb_put_file`, `akb_get_file`, `akb_delete_file`) work with local file paths —
they are handled by the akb-mcp stdio proxy which streams files directly to/from S3.

## Tools

| Tool | Description |
|------|-------------|
| `akb_put_file` | Upload a local file to vault storage |
| `akb_get_file` | Download a file to a local path |
| `akb_delete_file` | Delete a file |
| `akb_browse` | Files appear in unified browse |
| `akb_link` | Connect file to documents or tables |

## Upload
```
akb_put_file(vault="eng", file_path="/path/to/diagram.png", collection="diagrams",
  description="Architecture diagram")
# → {name: "diagram.png", s3_key: "eng/diagrams/abc_diagram.png", size_bytes: 45000}
```

## Download
```
akb_get_file(vault="eng", file_id="abc123", save_to="/tmp/downloads/")
# → {name: "diagram.png", save_to: "/tmp/downloads/diagram.png", size_bytes: 45000}
```

## Link a file to a document
```
akb_link(vault="eng",
  source="akb://eng/file/abc123",
  target="akb://eng/doc/specs/architecture.md",
  relation="attached_to")
```""",

    "akb_put_file": """# akb_put_file — Upload a Local File

Uploads a file from local disk to vault storage. Handled by akb-mcp proxy (streams directly to S3).

## Parameters
| Param | Required | Description |
|-------|----------|-------------|
| vault | ✓ | Vault name |
| file_path | ✓ | Absolute path to the local file |
| collection | | Logical grouping (e.g. 'diagrams') |
| description | | Brief description of the file |

## Example
```
akb_put_file(vault="eng", file_path="/path/to/report.pdf", collection="reports",
  description="Q1 analysis report")
→ {"name": "report.pdf", "collection": "reports", "size_bytes": 128000}
```

After upload, the file appears in `akb_browse` and can be linked with `akb_link`.""",

    "akb_link": """# akb_link — Connect Any Two Resources

Create a typed relation between documents, tables, and/or files using AKB URIs.

## Parameters
| Param | Required | Description |
|-------|----------|-------------|
| vault | ✓ | Vault name |
| source | ✓ | Source AKB URI |
| target | ✓ | Target AKB URI |
| relation | ✓ | depends_on, related_to, implements, references, attached_to, derived_from |

## URI Format
- `akb://{vault}/doc/{path}` — document
- `akb://{vault}/table/{name}` — table
- `akb://{vault}/file/{id}` — file

## Examples
```
# Link a document to a data table
akb_link(vault="eng",
  source="akb://eng/doc/reports/analysis.md",
  target="akb://eng/table/experiment-results",
  relation="references")

# Attach a file to a document
akb_link(vault="eng",
  source="akb://eng/file/diagram-abc123",
  target="akb://eng/doc/specs/architecture.md",
  relation="attached_to")

# Mark data lineage
akb_link(vault="eng",
  source="akb://eng/table/summary-stats",
  target="akb://eng/table/raw-data",
  relation="derived_from")
```

💡 Browse results include `uri` for each item — use those directly.""",

    "akb_unlink": """# akb_unlink — Remove a Relation

## Parameters
| Param | Required | Description |
|-------|----------|-------------|
| vault | ✓ | Vault name |
| source | ✓ | Source AKB URI |
| target | ✓ | Target AKB URI |
| relation | | Specific type to remove (omit = remove ALL between source/target) |

## Examples
```
akb_unlink(vault="eng",
  source="akb://eng/doc/specs/api.md",
  target="akb://eng/table/endpoints",
  relation="references")

akb_unlink(vault="eng",
  source="akb://eng/doc/specs/api.md",
  target="akb://eng/table/endpoints")  # removes ALL relations
```""",

}



def _resolve_help(topic: str | None) -> str:
    """Resolve help topic with fuzzy matching."""
    if topic is None:
        return HELP[None]

    t = topic.strip().lower()

    # Exact match
    if t in HELP:
        return HELP[t]

    # Try with akb_ prefix
    if not t.startswith("akb_") and f"akb_{t}" in HELP:
        return HELP[f"akb_{t}"]

    # Fuzzy: find topics containing the query
    matches = [k for k in HELP if k and t in k]
    if len(matches) == 1:
        return HELP[matches[0]]
    if matches:
        listing = "\n".join(f"- `{m}`" for m in sorted(matches))
        return f"# Multiple matches for \"{topic}\"\n\nDid you mean one of these?\n{listing}\n\nUse `akb_help(topic=\"...\")` with the exact name."

    # List all available topics
    categories = [k for k in HELP if k and not k.startswith("akb_") and "-" not in k]
    workflows = [k for k in HELP if k and "-" in k and not k.startswith("akb_")]
    tools = [k for k in HELP if k and k.startswith("akb_")]

    return f"""# No help found for "{topic}"

## Available topics

**Categories:** {", ".join(sorted(categories))}

**Workflows:** {", ".join(sorted(workflows))}

**Tools:** {", ".join(sorted(tools))}

Use `akb_help(topic="...")` with any of the above."""


# ── Tool Handlers ────────────────────────────────────────────
