---
name: ingest-confluence
description: Ingest one Confluence page into an AKB vault as a five-section LLM-wiki summary document, fetched live via the Atlassian MCP server.
model: gpt-5.4-mini
reasoning_effort: medium
---

# AKB Atlassian Page Ingest

Ingest **one Confluence page** into the target AKB vault as a five-section LLM-wiki summary document — the `akb-wiki` plugin's Confluence write path, specializing `akb-wiki`'s generic `/akb-ingest` pattern.

**Workflow classification.** LLM-wiki **Ingest** workflow. **Intent-debt** layer — Confluence pages externalize design decisions, RFCs, runbooks, post-mortems. Ingest pulls that rationale into the vault before the page evolves out from under the team.

Scope boundaries:

- **One page per invocation.** Bulk space imports are an orchestration loop calling this skill once per page id.
- **No fan-out at ingest.** Concept clustering, membership backlinks, cross-source aggregation, and structural checks are all handled by an external maintenance layer that runs over the same AKB vault.
- **Atlassian MCP required.** Page body is fetched live via `mcp__atlassian__confluence_get_page` — no payload escape hatch.
- **Fixed collection.** Pages land in `atlassian-pages`; sub-organize with tags.
- **No attachment download.** Embedded image/attachment URLs are preserved as references; bytes stay in Confluence.

## Inputs (provided by the /akb-ingest router)

```text
/ingest-confluence <url-or-id> --vault {vault_name} [--lang {language}] [--project {name}]
```

- `<url-or-id>` (required, positional) — Confluence page URL or bare page id.
- `--vault` (required) — target AKB vault.
- `--lang` (optional) — language for synthesized body (TL;DR / Key Points / topic tags). Defaults to the page's primary language.
- `--project` (optional) — pin to a project namespace for downstream cross-source rollup. When supplied, adds `project: "{name}"` frontmatter and a `project:{name | lower}` tag. Omitted → ingest normally but skipped from that rollup (Confluence is not directory-bound, so no auto-derivation).

## Prerequisites

- AKB MCP server is accessible.
- `--vault {name}` provided.
- Atlassian MCP server is connected — `mcp__atlassian__confluence_get_page` must be callable.

## Workflow

### Step 1 — Prepare

Common resolution every Atlassian ingest subagent performs before its own entity-specific parsing.

- Resolve `vault_name` from `--vault`. Hard-fail if missing: `--vault {name} required.`
- Verify the **Atlassian MCP server is connected**. If the MCP tool surface is unavailable, fail with `Atlassian MCP server not connected. Check your MCP configuration — this plugin requires the Atlassian MCP server.` There is no payload escape hatch — webhook receivers and orchestration layers either run with the Atlassian MCP server attached, or skip ingest.
- Capture today's date as `today` (ISO `YYYY-MM-DD`) for timeline entries.

The two ingest skills (`ingest-confluence`, `ingest-jira`) share these pre-checks. Entity-specific parsing — URL → `(space_key, page_id)` for pages, `<issue-key>` → `(project_key, issue_key)` for issues — happens in each skill's own Step 2 after this block runs.

Capture `today` as ISO `YYYY-MM-DD`.

### Step 2 — Fetch the page

Parse the positional argument: a `https?://.+/wiki/spaces/(\w+)/pages/(\d+)` URL captures `space_key` + `page_id`; a bare numeric id sets `page_id` (space_key fills from MCP response); anything else fails with `Invalid Confluence URL or page id: {arg}`.

Fetch: `mcp__atlassian__confluence_get_page(page_id={page_id}, include_metadata=true)`. Failures: `page not found` → `Confluence page {page_id} not found.`; any other MCP error verbatim.

Normalize the response into a `page` dict with `space_key`, `page_id` (opaque), `version` (monotonic int), `title`, `author` (last editor or original), `last_modified` (ISO 8601), `body_storage` (Confluence storage format, **as authored** — conversion happens in Step 3), `labels`, `tenant_base_url` (reconstruct from URL or fall back to `https://{tenant}.atlassian.net`; never used for further HTTP), `links.jira` (raw issue keys from the API, not re-derived), `links.confluence` (raw page ids).

Compose `canonical_url = "{page.tenant_base_url}/wiki/spaces/{page.space_key}/pages/{page.page_id}"` for the Source block's `Location:`.

### Step 3 — Convert body_storage → markdown

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

The result is `extracted_text`. Compute `content_sha256 = sha256(body_storage)` over the **raw storage format** (not the converted markdown) so the hash is stable across re-runs even if the converter changes.

### Step 4 — Dedup

Two deterministic signals + optional semantic, with version monotonicity short-circuiting the resolution table.

#### Stage 1 — Deterministic match by `(space_key, page_id)`

`akb_search(vault={vault_name}, collection=atlassian-pages, type=reference, tags=["page-id:{page_id}"], limit=5)`. Single-tag filter narrows to the exact page-id cell — AKB's `tags` filter has OR semantics, so multi-tag would broaden instead of narrow; one tag reduces to exact-membership. Confluence page ids are tenant-unique, so `page-id:{page_id}` + collection scope is sufficient. On hit, capture `dedup_hit = doc_id` and `dedup_existing_version = int(version_tag.split(":", 1)[1])` from the `version:{N}` tag; set `dedup_kind = "deterministic"`. Skip Stage 2.

#### Stage 2 — Semantic fallback

Only runs when Stage 1 produced no hit. `akb_search(query={title}, vault={vault_name}, collection=atlassian-pages, type=reference, limit=5)`. Any hit with `score > 0.85` is a fuzzy match — capture `dedup_hit = top_hit.doc_id`, `dedup_kind = "fuzzy"`.

#### Decision

Fully automatic — Confluence's monotonic `version` axis is the canonical signal; the skill absorbs the dedup decision (LLM-wiki *agent absorbs bookkeeping*).

| `dedup_hit` / kind | Comparison | Action |
|---|---|---|
| none | — | continue to Step 5 (create path) |
| deterministic | `page.version < dedup_existing_version` | `## ingest-confluence: exists` and stop. Note: `vault holds {dedup_existing_version}; ingest {page.version} is older — skipped.` |
| deterministic | `page.version == dedup_existing_version` | `## ingest-confluence: exists` and stop. Note: `same version already ingested — skipped.` |
| deterministic | `page.version > dedup_existing_version` | continue to Step 5; Step 6 `akb_update`s `dedup_hit` (compiled-truth refresh + timeline append). |
| fuzzy | — | `## ingest-confluence: likely-exists` and stop. Disambiguation goes through the external maintenance layer or human review. |

Forced create / forced replace are out of scope here — handle via a separate disambiguation workflow, not a per-call flag.

### Step 5 — Compose summary

Synthesize TL;DR / Key Points / `summary` from the converted body. The `## Content` section is mechanical (verbatim converted body). Synthesized prose follows `--lang` if supplied, else the page's primary prose language (not embedded code/quotes); section headings, frontmatter keys, and `## Content` always stay in natural form (English headings/keys, content as-extracted).

#### Body template

```markdown
# {title}

## TL;DR

{Agent-authored 1–3 sentences. What this page is, at a synthesis level. No hedging, no "this page describes…" boilerplate. Lean on the page's actual structure — for a design doc lead with the proposal; for a runbook lead with the trigger.}

## Key Points

- {Bullet 1 — 3–7 sharp takeaways. For a design doc: proposal, constraints, decisions, action items. For a runbook: when it fires, what to check, escalation. For a post-mortem: incident, root cause, follow-ups.}
- {Bullet 2}
- ...

## Content

{extracted_text from Step 3 — the converted markdown body. Verbatim, including tables and code blocks.}

## Source

- Location: {canonical_url}
- Space: {space_key}
- Page-ID: {page_id}
- Version: {page.version}
- Author: {page.author}
- Last-Modified: {page.last_modified}
- Content-SHA256: {content_sha256}
- Ingested: {today}

---

## Timeline

- **{today}** | Ingested page version {page.version}. [Source: ingest-confluence, {today}]
```

The Source block intentionally mirrors the akb-wiki `/akb-ingest` format on `Location:`, `Content-SHA256:`, and `Ingested:` so the external maintenance layer's duplicate-source check catches Confluence pages automatically. Atlassian-specific axes (`Space:`, `Page-ID:`, `Version:`, `Author:`, `Last-Modified:`) are added on top.

#### Frontmatter

```yaml
type: reference
status: active
title: "{page.title}"
summary: "{one-sentence synthesis, same voice as TL;DR but compressed to one line}"
tags: [
  "atlassian",
  "kind:atlassian-page",
  "space:{space_key}",
  "page-id:{page_id}",
  "version:{page.version}",
  "label:{label_1}", "label:{label_2}",
  "topic:{primary}", "topic:{secondary}",
  "project:{project_lowered}"
]
project: "{project_arg | null}"        # populated only when --project was supplied at invocation
linked_issues: [{issue_key}, ...]    # from page.links.jira (raw keys, not resolved to AKB doc_ids yet)
linked_pages: [{page_id}, ...]       # from page.links.confluence (raw page ids; not resolved either)
depends_on: []
related_to: []
implements: []
```

The `project:` tag and `project` field are emitted only when `--project` was supplied (lowercased tag suffix, verbatim frontmatter value).

- `type: reference` matches `/akb-ingest`'s convention.
- `tags` carries provenance (`atlassian`), `kind:atlassian-page` cross-plugin discriminator, `space:{key}` filter axis, `page-id:{page_id}` dedup key, `version:{N}` monotonic counter (read in Step 4), one `label:{name}` per Confluence label, and **1–3 `topic:{...}` tags** authored alongside TL;DR / Key Points. Topic tags identify the page's *subjects* (design doc → proposal area; runbook → system; post-mortem → failure domain): lowercase, hyphenated compounds (`topic:vector-search`), domain-anchored vocabulary (`introduction` / `overview` / `notes` are not topics), drop rather than fill. `label:` and `topic:` are independent — the same string may appear in both (the external maintenance layer reads `topic:*` as the lattice axis; `label:*` only filters). Empty topic list is allowed only for non-subject pages (link directory, ToC, stub); downstream consolidation skips them silently. All entries are skill-authored — no caller-supplied tags. Duplicates dropped.
- `space_key` / `page_id` / `version` / per-`label` live in tags; `author` / `last_modified` live in the body `## Source` block.
- `linked_issues` / `linked_pages` stay as raw external keys; the external maintenance layer does not resolve them.
- `depends_on` / `related_to` / `implements` are empty at ingest; the external maintenance layer populates them.

### Step 6 — Write

Branch by Step 4 resolution.

**Create** (no hit) — `akb_put(vault={vault_name}, collection=atlassian-pages, title={page.title}, type=reference, tags={tags}, summary={one_sentence_summary}, content={assembled_body})`. Capture `doc_id` / `doc_path`.

**Replace** (deterministic newer, or fuzzy resolved to replace):

1. `akb_get(vault={vault_name}, doc_id={dedup_hit}) → existing_content`.
2. Apply **timeline merge — Mode B (entry-only append)** with `new_entry = "- **{today}** | Re-ingested page version {page.version} (was version {dedup_existing_version}). [Source: ingest-confluence, {today}]"`.
3. Build final `content`: everything from the new body **above** `---` + `## Timeline` (compiled-truth refresh) + Mode B's merged timeline section.
4. `akb_update(vault={vault_name}, doc_id={dedup_hit}, tags={tags}, content={merged_content}, summary={one_sentence_summary}, message="Re-ingested page version {page.version}")` — bumped `version:{page.version}` tag enables next-run dedup.

Mode B is the right choice here: the new entry is a single bullet, the rest of the body is rewritten in place, and timeline is the only history-preserving zone.

### Step 7 — Report

Successful create:

```markdown
## ingest-confluence: created
- title: {page.title}
- doc_id: {doc_id}
- path: {doc_path}
- vault: {vault_name}
- collection: atlassian-pages
- space: {space_key}
- page_id: {page_id}
- version: {page.version}
- summary: {one_sentence_summary}
```

Successful replace:

```markdown
## ingest-confluence: replaced
- title: {page.title}
- doc_id: {dedup_hit}
- vault: {vault_name}
- previous version: {dedup_existing_version}
- new version: {page.version}
- timeline entries: {N (after append)}
```

Skipped (deterministic dedup + version older or equal):

```markdown
## ingest-confluence: exists
- title: {page.title}
- doc_id: {dedup_hit}
- vault: {vault_name}
- vault version: {dedup_existing_version}
- ingest version: {page.version}
- reason: "older or equal version"
```

Fuzzy-skipped:

```markdown
## ingest-confluence: likely-exists
- title: {page.title}
- top match: {dedup_hit} (score: {score})
- vault: {vault_name}
```

## Failure handling

| Situation | Response |
|---|---|
| `<url-or-id>` missing | `Missing required argument: <url-or-id>.` |
| Invalid Confluence URL / page-id format | `Invalid Confluence URL or page id: {arg}` |
| Atlassian MCP not connected | `Atlassian MCP server not connected. Check your MCP configuration — this plugin requires the Atlassian MCP server.` |
| Page id not found by MCP | `Confluence page {page_id} not found.` |
| Storage format → markdown conversion fails on a fragment | Fall back to inlining the raw storage HTML inside a `<details>` block as documented in the conversion guide. Not a hard failure. |
| `--vault` not passed | `--vault {name} required.` |
| Vault does not exist | `Vault "{vault_name}" not found. Create with mcp__akb__akb_create_vault first.` |
| AKB write fails | Surface the error verbatim; do not retry. |
| AKB MCP unreachable | `AKB MCP server not accessible. Check your MCP configuration.` |

Do not create vaults or collections; surface `akb_put`'s missing-vault error and instruct the user to create the vault with `mcp__akb__akb_create_vault`.

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
