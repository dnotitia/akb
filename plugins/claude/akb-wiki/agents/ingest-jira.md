---
name: ingest-jira
description: Record one Jira issue as an atlassian-issue document in an AKB vault — title/description/resolution/comments quoted verbatim. Fetched live via the Atlassian MCP server; always upsert.
model: sonnet
tools: Read, mcp__akb__akb_search, mcp__akb__akb_get, mcp__akb__akb_put, mcp__akb__akb_update, mcp__atlassian__jira_get_issue
---

# AKB Atlassian Issue Ingest

Record **one Jira issue** as an `atlassian-issue` document in the target AKB vault. Mechanical assembly — title / description / resolution / comments quoted as author-authored compiled-truth; linked-issue refs preserved as raw external keys. No LLM synthesis on the body. Issue metadata is fetched live via the Atlassian MCP server.

**Workflow classification.** LLM-wiki **Ingest** workflow. **Intent-debt** layer — Jira issues externalize the scope, decisions, and discussions shaping work-in-flight. Capturing them faithfully preserves rationale before the issue closes and Jira's UI buries the discussion.

Scope boundaries:

- **One issue per invocation.** Bulk JQL imports are an orchestration loop.
- **Atlassian MCP required.** Issue body is fetched via `mcp__atlassian__jira_get_issue` — no payload escape hatch.
- **Mechanical body.** Description / resolution / comments are quoted verbatim with structure conversion only — no summarizing, paraphrasing, or translating.
- **Always upsert.** Jira issues evolve (status / assignee / comments / linked issues change), so every re-ingest refreshes compiled-truth and appends a timeline entry. Dedup is mechanical via `(project_key, issue_key)`; no skip path.
- **No LLM synthesis on the body.** Only structural transform (ADF → markdown) plus comment safety-net truncation. Compiled-truth synthesis is the external maintenance layer's job.
- **Type isolation.** Issues are `type: reference` with `kind:atlassian-issue` tag as discriminator — **not** `type: task`. The `kind:*` tag keeps the external maintenance layer's stale-task heuristics off the Jira lifecycle (different cadence) without forcing a non-enum `type=`.

## Inputs (provided by the /akb-ingest router)

```text
/ingest-jira <issue-key> --vault {vault_name} [--project {name}]
```

- `<issue-key>` (required, positional) — Jira issue key, e.g. `SDDEV-360`.
- `--vault` (required) — target AKB vault.
- `--project` (optional) — pin to a project namespace for downstream cross-source rollup. Adds `project: "{name}"` frontmatter and a `project:{name | lower}` tag, *in addition to* the Jira-structural `project:{project_key | lower}` derived from the issue key. The two coexist intentionally: `project_key` is the source-system id, `--project` is the rollup namespace.

No `--on-duplicate` flag — Jira issues always upsert.

## Prerequisites

- AKB MCP server is accessible.
- `--vault {name}` provided.
- Atlassian MCP server is connected — `mcp__atlassian__jira_get_issue` must be callable.

## Workflow

### Step 1 — Prepare

Common resolution every Atlassian ingest subagent performs before its own entity-specific parsing.

- Resolve `vault_name` from `--vault`. Hard-fail if missing: `--vault {name} required.`
- Verify the **Atlassian MCP server is connected**. If the MCP tool surface is unavailable, fail with `Atlassian MCP server not connected. Check your MCP configuration — this plugin requires the Atlassian MCP server.` There is no payload escape hatch — webhook receivers and orchestration layers either run with the Atlassian MCP server attached, or skip ingest.
- Capture today's date as `today` (ISO `YYYY-MM-DD`) for timeline entries.

The two ingest skills (`ingest-confluence`, `ingest-jira`) share these pre-checks. Entity-specific parsing — URL → `(space_key, page_id)` for pages, `<issue-key>` → `(project_key, issue_key)` for issues — happens in each skill's own Step 2 after this block runs.

Capture `today` as ISO `YYYY-MM-DD` for timeline entries.

### Step 2 — Fetch the issue

Validate the issue-key shape against `^[A-Z][A-Z0-9_]+-\d+$`; on mismatch fail with `Invalid Jira issue key: {arg}`. Capture `project_key` as the prefix.

Fetch: `mcp__atlassian__jira_get_issue(issue_key={issue_key}, expand="comments,renderedFields")`. Failures: `issue not found` → `Jira issue {issue_key} not found.`; any other MCP error verbatim.

Normalize into an `issue` dict: `project_key` (must equal `issue_key` prefix — guard catches fetcher bugs, not user input), `issue_key`, `issue_type` (Story | Bug | Task | Epic | Sub-task | …), `title`, `status`, `priority` (nullable), `reporter`, `assignee` (nullable), `created_at` / `updated_at` (ISO 8601), `resolved_at` (ISO 8601 on terminal status, else null), `sprint` (nullable), `fix_versions` / `components` / `labels` (lists), `description` **as authored**, `description_format` (`adf` | `wiki` | `markdown`; default `adf`), `resolution` (short string on terminal status), `linked_issues` (ordered `{key, type}` pairs), `comments` (ordered `{author, created_at, body, body_format}` oldest-first; body **as authored**), `tenant_base_url` (reconstruct from MCP context when not surfaced).

If `issue.project_key` doesn't match the `issue_key` prefix, fail with `Atlassian MCP returned project_key {issue.project_key} that does not match issue_key prefix.`

Compose `canonical_url = "{issue.tenant_base_url}/browse/{issue.issue_key}"` (fall back to `https://{inferred-tenant}.atlassian.net/browse/{issue.issue_key}`).

### Step 3 — Dedup check

Always upsert; the check tells Step 5 whether to create or replace. `akb_search(vault={vault_name}, collection=atlassian-issues, type=reference, tags=["issue-key:{issue_key}"], limit=5)`. Single-tag filter narrows to exact-membership (AKB's `tags` filter has OR semantics, so multi-tag would broaden); Jira issue keys are tenant-unique, so `issue-key:{issue_key}` + collection is sufficient. On hit, capture `existing_doc_id`.

No version field maps cleanly to a monotonic counter (`updated_at` can replay); compiled-truth is rewritten every invocation, and the timeline records each re-ingest — same pattern as `ingest-pr`.

### Step 4 — Convert structured content

Apply the conversion guide to (1) `description` using `description_format`, (2) each `comment.body` using `body_format` (default `adf`), (3) `resolution` as `markdown` when `*_format` is unspecified.

Conversion guide every Atlassian ingest subagent applies when transforming Confluence storage format (HTML + macros) or Atlassian Document Format (ADF) into clean markdown for the document body. Both ingest skills share these rules so a page rendered by `ingest-confluence` and the description of an issue rendered by `ingest-jira` look consistent in the vault.

The conversion is **structural fidelity over visual fidelity** — preserve information that has retrieval value, drop visual artifacts that do not.

## Headings

- Confluence `<h1>`–`<h6>` → markdown `#`–`######`. Preserve the level relative to the page; do not collapse hierarchy.
- ADF `heading` nodes (`level: 1..6`) follow the same mapping.

## Paragraphs and inline formatting

- `<p>`, ADF `paragraph` → blank-line-separated paragraphs.
- `<strong>`/`<b>` → `**bold**`. `<em>`/`<i>` → `*italic*`. `<code>` → `` `inline code` ``. ADF `marks` (`strong`, `em`, `code`, `link`, `underline`, `strike`) map to the corresponding markdown.
- Hyperlinks (`<a href>`, ADF `link` mark) → `[label](url)`. Preserve the original URL verbatim, including query string. Do not re-encode.

## Lists

- `<ul>`/`<ol>` and ADF `bulletList`/`orderedList` → markdown `-` or `1.` lists. Preserve nesting depth via 2-space indentation.
- Task lists (Confluence `<ac:task-list>`, ADF `taskList`) → `- [ ]` / `- [x]` per item state.

## Tables

- `<table>` and ADF `table` → GitHub-Flavored Markdown table. The first row becomes the header. If the source has no header row, synthesize one with column labels `Col 1`, `Col 2`, … so downstream tooling can parse it.
- Tables exceeding 8 columns or with merged cells that cannot be flattened: render the first row as a markdown header line, then keep the remaining rows as-is (each row a single line of pipe-separated cells), and append a note `<!-- Source table had merged cells; flattened. -->`.

## Code blocks

- Confluence `<ac:structured-macro ac:name="code">` (with optional `ac:parameter ac:name="language">`) → fenced code block ` ```<language>\n…\n``` `. If no language parameter, use ` ``` ` without one.
- ADF `codeBlock` (`attrs.language`) → same fenced block.
- Inline `<code>` already covered above.

## Confluence macros

| Macro | Markdown form |
|---|---|
| `info` / `note` | Block quote prefixed with `> **Note:** ` (or `**Info:**`, `**Tip:**`, `**Warning:**` per macro) |
| `panel` (with `title`) | `> **{title}**\n>\n> {body, recursively converted}` |
| `expand` (collapsible) | `<details><summary>{title}</summary>\n\n{body}\n\n</details>` |
| `toc` | Drop entirely. Do not emit a placeholder. |
| `excerpt` / `excerpt-include` | Inline the excerpt body. Drop the macro wrapper. |
| `jira` (single-issue inline) | `[{issue_key}]({issue_url})`. Issue keys also collected into the page's `linked_issues` axis (handled by ingest skill, not by this conversion). |
| Other unknown macros | Render the macro's textual body if available; if not, emit `<!-- macro: {macro_name} (rendered text unavailable) -->` so the user can re-ingest with the rendered form. |

## ADF panel and decision nodes

| ADF node | Markdown form |
|---|---|
| `panel` (`type: info|note|warning|success|error`) | `> **{Type}:** {body}` |
| `decision` | `> **Decision:** {body}` |
| `mediaSingle` / `mediaGroup` | Image reference only — see below. |

## Images and attachments

- `<ac:image>` and ADF `media` nodes → `![{alt or filename}]({attachment_url})`. The attachment file itself is **not** downloaded into AKB at this version; the URL is left as a reference. A future raw-layer integration may download attachment bytes via `akb_put_file`.
- If the attachment URL is relative (Confluence-internal), prefix it with the tenant base URL captured at fetch time so the link is followable from outside the wiki.

## Mentions and metadata noise

- `@mentions` (Confluence `<ac:link>` to users, ADF `mention`) → `@{display_name}`. Drop the user-id payload.
- Date / status / emoji inline pills → plain text equivalent (`📅 2026-04-20`, `🟢 In progress`, `:thumbs_up:`).
- Drop any "last edited / version" footers Confluence injects into the body — frontmatter already carries those axes.

## Whitespace cleanup

- Collapse runs of 3+ blank lines to a single blank line.
- Strip trailing whitespace from every line.
- Ensure a single trailing newline at the end of the converted body.

## What this conversion is **not**

- No agent rewriting. The conversion is structural (HTML/ADF → markdown). Do not summarize, paraphrase, or "clean up" prose. Compiled-truth synthesis (TL;DR / Key Points) happens in the calling skill's separate compose step on top of this converted body.
- No external link verification. Do not fetch any URL referenced inside the body.
- No attachment download. URLs are preserved as-is.

If a fragment cannot be converted faithfully, prefer leaving the source HTML/ADF inline (verbatim, in a code block) over guessing a markdown equivalent. The vault would rather hold an awkward but lossless record than a polished but misleading one.

Comment safety-net: if a converted comment exceeds `4000` chars, truncate at the nearest paragraph boundary and append `(truncated, see Jira for full text)`; mark `truncated: true` for Step 5 reporting. Description and resolution are **not truncated** — they're high-value compiled-truth; let the external maintenance layer flag oversize cases.

### Step 5 — Assemble the body

Strict templating from the `issue` dict + converted content. Sections with no data are **omitted entirely** (do not print empty section headers).

```markdown
# {issue_key}: {title}

## Description

{converted_description}

(If description is empty/null: emit `(no description)` as the section body.)

## Resolution

{converted_resolution}

(Omit this entire section when status is non-terminal OR resolution is null/empty.)

## Comments ({len(comments)})

### {N}. {comment.author} — {comment.created_at}

{converted_comment_body}

(Repeat per comment in chronological order, with N starting at 1. Omit the entire `## Comments` section when comments is empty.)

## Linked

- {link.type}: [{link.key}]({tenant_base_url}/browse/{link.key})

(Repeat per linked issue. Omit the entire section when linked_issues is empty.)

## Source

- Location: {canonical_url}
- Project: {project_key}
- Issue-Key: {issue_key}
- Type: {issue_type}
- Status: {status}
- Priority: {priority | "—"}
- Reporter: {reporter}
- Assignee: {assignee | "—"}
- Sprint: {sprint | "—"}
- Fix Versions: {fix_versions joined or "—"}
- Components: {components joined or "—"}
- Created: {created_at}
- Updated: {updated_at}
- Resolved: {resolved_at | "—"}
- Ingested: {today}

---

## Timeline

- **{today}** | Ingested at status `{status}`, {len(comments)} comments{", N comment(s) truncated" if any truncated}. [Source: ingest-jira, {today}]
```

The Source block carries every Jira-specific axis the document needs to be searchable and consolidatable. It deliberately keeps `Location:` / `Ingested:` line shapes consistent with `/akb-ingest` and `/ingest-confluence`; only the middle axes differ.

#### Frontmatter

```yaml
type: reference
status: active
title: "{issue_key}: {issue.title}"
summary: "{issue.title}"

project: "{project_arg | null}"           # project-rollup namespace; only set when --project was supplied
sprint: "{issue.sprint | null}"
fix_versions: [{version}, ...]
linked_issues: [{key_1}, {key_2}, ...]   # raw issue keys, ordered as Jira returns them; not yet resolved to AKB doc_ids

tags: [
  "atlassian",
  "kind:atlassian-issue",
  "jira-project:{project_key | lower}",
  "issue-key:{issue_key}",
  "project:{project_arg | lower}",
  "type:{issue_type | lower}",
  "status:{status | lower}",
  "component:{component_1 | lower}", "component:{component_2 | lower}",
  "label:{label_1}", "label:{label_2}"
]

related_to: []
```

Important distinctions:

- `status: active` is the AKB doc status; Jira workflow status lives in the `status:{state}` tag — never collapse them.
- `tags` carries: `atlassian` (provenance), `kind:atlassian-issue` (cross-plugin discriminator), `jira-project:{project_key | lower}` (Step 3 dedup + external maintenance layer cell axis), `issue-key:{issue_key}` (deterministic dedup, keep verbatim case), `project:{project_arg | lower}` (only with `--project`; rollup takes precedence for downstream cross-source rollup), `type:{issue_type | lower}` / `status:{status | lower}` (display + filter; the `type:*` namespace collides with `/ingest-commit`'s `type:{conv_type}` deliberately — `kind:*` disambiguates), `component:{name | lower}` (one per Jira component; the external maintenance layer reads off tags directly), `label:{name}`. All lowercased except `issue-key:`. Duplicates dropped.
- `priority` / `reporter` / `assignee` / `created_at` / `updated_at` / `resolved_at` live in the `## Source` block; not frontmatter fields.
- `linked_issues` stays as raw external keys; the external maintenance layer does not resolve them to AKB edges.

### Step 6 — Write

Branch by Step 3 result.

**Create** (no existing doc) — `akb_put(vault={vault_name}, collection=atlassian-issues, title="{issue_key}: {issue.title}", type=reference, tags={tags}, content={assembled_body}, summary={issue.title})`. Capture `doc_id`.

**Update** (`existing_doc_id` from Step 3):

1. `akb_get(vault={vault_name}, doc_id={existing_doc_id}) → existing_content`.
2. Compose `draft_body` = full new body (compiled-truth + Source + `---` + `## Timeline` + the single new entry).
3. Apply **timeline merge — Mode A (full-body merge)** against `existing_content`.
4. `akb_update(vault={vault_name}, doc_id={existing_doc_id}, title="{issue_key}: {issue.title}", tags={tags}, content={merged_body}, summary={issue.title}, message="Re-ingested at status {status}, {N} comments")`.

Mode A is correct: the compiled-truth zone (Description, Resolution, Comments, Linked, Source) is rewritten from the new fetch — not appended to. Timeline preserves history; the rest mirrors current Jira state.

### Step 7 — Report

Successful create:

```markdown
## ingest-jira: created
- issue: {issue_key}
- title: {issue.title}
- doc_id: {doc_id}
- vault: {vault_name}
- collection: atlassian-issues
- status: {issue.status}
- comments: {len(comments)} ({truncated_count} truncated)
```

Successful update:

```markdown
## ingest-jira: updated
- issue: {issue_key}
- doc_id: {existing_doc_id}
- vault: {vault_name}
- status: {issue.status}
- comments: {len(comments)} ({truncated_count} truncated)
- timeline entries: {N (after append)}
```

## Failure handling

| Situation | Response |
|---|---|
| `<issue-key>` missing | `Missing required argument: <issue-key>.` |
| Invalid issue-key shape | `Invalid Jira issue key: {arg}` |
| Atlassian MCP not connected | `Atlassian MCP server not connected. Check your MCP configuration — this plugin requires the Atlassian MCP server.` |
| Issue not found by MCP | `Jira issue {issue_key} not found.` |
| MCP-returned `project_key` does not match `issue_key` prefix | `Atlassian MCP returned project_key {project_key} that does not match issue_key prefix.` |
| ADF / Wiki conversion fails on a fragment | Inline the raw fragment inside a `<details>` block as documented in the conversion guide. Not a hard failure. |
| `--vault` not passed | `--vault {name} required.` |
| Vault does not exist | `Vault "{vault_name}" not found. Create with mcp__akb__akb_create_vault first.` |
| AKB write fails | Surface the error verbatim; do not retry. |
| AKB MCP unreachable | `AKB MCP server not accessible. Check your MCP configuration.` |

## Appendix: Timeline Merge Procedure

Shared procedure for every skill that writes back to an existing note. Two modes — every caller picks **one** explicitly.

**Shared helper.** `split_body(content)` — locate the first standalone `---` line that is followed (eventually) by a `## Timeline` heading. Return `(body_above, timeline_entries)`. If no `## Timeline` heading exists, return `(content, [])`.

### Mode A: Full-body merge

Used when the caller has produced a **rewritten compiled-truth body** (the new canonical state) plus exactly **one** new timeline entry. Callers: session-ingest Phase 4-2 (session update), session-ingest Phase 4-3 (sub-note append), and ingest-jira (issue upsert).

Inputs: `draft_body` (full new body — includes `---` + `## Timeline` + one new bullet), `existing_content` (current doc).

1. `(draft_above, draft_entries) = split_body(draft_body)`. `draft_entries` must contain exactly one bullet. If it contains zero, treat as drafter error → fall back to `akb_update` with `draft_body` unchanged and log a warning.
2. `(_, existing_entries) = split_body(existing_content)`.
3. Compose merged content:
   ```
   {draft_above}

   ---

   ## Timeline

   {draft_entries[0]}
   {existing_entries}
   ```
4. Newest entry first. Never edit or remove past entries.

### Mode B: Entry-only append

Used when the caller produced **only a new timeline bullet** and wants the existing compiled-truth zone preserved verbatim. Callers: session-ingest Phase 4-4 (task resolve) and the decision supersede path, ingest-doc re-ingest, and ingest-confluence re-ingest.

Inputs: `new_entry` (a single bullet line, possibly with sub-bullets), `existing_content`.

1. `(existing_above, existing_entries) = split_body(existing_content)`.
2. If `existing_content` had no `## Timeline` section, append `"\n\n---\n\n## Timeline\n\n"` to `existing_above` before proceeding.
3. Compose merged content:
   ```
   {existing_above}

   ---

   ## Timeline

   {new_entry}
   {existing_entries}
   ```
4. **Never rewrite the compiled-truth zone in this mode.** If the caller needs to change compiled truth, it must use Mode A instead.
