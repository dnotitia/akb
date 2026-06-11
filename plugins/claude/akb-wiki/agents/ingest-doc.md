---
name: ingest-doc
description: Ingest one document (local file or web URL) into an AKB vault as a five-section LLM-wiki summary page, optionally preserving the original bytes in the raw file layer.
model: sonnet
tools: Read, Glob, WebFetch, Bash(curl *), Bash(rm *), mcp__akb__akb_search, mcp__akb__akb_get, mcp__akb__akb_put, mcp__akb__akb_put_file, mcp__akb__akb_update, mcp__akb__akb_link, mcp__akb__akb_relations, mcp__akb__akb_browse
---

# AKB Ingest

Ingest **one document** (local path or URL) into the target AKB vault as a five-section summary page, optionally preserving the original bytes in AKB's raw file layer. This skill is the `akb-wiki` plugin's ingest write path.

**Workflow classification.** LLM-wiki **Ingest** workflow. **Intent-debt** layer — it externalizes documents of record (papers, articles, manuals, transcripts, web clips) into the vault so the rationale and constraints in those sources stay retrievable by humans and agents.

Scope boundaries:

- **One source per invocation.** Glob patterns expand into a sequential loop that calls the same workflow once per match.
- **No fan-out at ingest time.** Consolidation into concept pages and cross-reference to existing entities are handled by an external maintenance layer (dream-cycle) that runs over the same AKB vault, not at ingest time. AKB's own search/browse already indexes every write, so the skillpack does not maintain a manual vault-index document.
- **2-layer write when binary.** Binary formats upload their original bytes via `akb_put_file` **and** emit a summary document with a `derived_from` link to the raw file. Plaintext formats emit only the summary.
- **Fixed collections.** Summary documents always land in `corpus-summaries`, raw files in `corpus-raw`. No `--collection` override — use tags for sub-organization.

## Inputs (provided by the /akb-ingest router)

```text
/ingest-doc <source> --vault {vault_name} [--raw | --no-raw] [--lang {language}]
```

- `<source>` (required) — local path (absolute, or glob like `./papers/*.pdf`) or URL (http/https).
- `--vault` (required) — target AKB vault name.
- `--raw` / `--no-raw` (optional) — override format-based raw-upload default (see Step 2).
- `--lang` (optional) — language for synthesized note body (TL;DR / Key Points / topic tag wording). If omitted, infer from the source's primary language. Use this to override when the source language differs from the desired note language (e.g., English paper → Korean summary).

## Prerequisites

- AKB MCP server is accessible.
- `--vault {name}` provided.
- For URL sources: `WebFetch` available.

## Workflow

### Step 1 — Prepare

Parse `--vault` from arguments. Hard-fail if missing: `--vault {name} required.`

Parse remaining CLI arguments. Capture today's date as `today` (ISO `YYYY-MM-DD`).

### Step 2 — Resolve source

Classify `<source>`:

- **Path** — starts with `/`, `./`, `~/`, or has no scheme. If it contains glob metacharacters (`*`, `?`, `[`), resolve with `Glob` and loop through each match, running Steps 3–7 per file. A zero-match glob fails with `No files matched "{pattern}".`
- **URL** — starts with `http://` or `https://`.

Detect format from extension (path) or `Content-Type` header when available (URL), plus filename hint from the URL path:

| Format | Extension set | Upload default |
|---|---|---|
| `pdf` | `.pdf` | binary → raw |
| `image` | `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp` | binary → raw |
| `office` | `.docx`, `.xlsx`, `.pptx` | binary → raw |
| `markdown` | `.md`, `.markdown` | plaintext → summary only |
| `html` | `.html`, `.htm`, URL with no extension | plaintext → summary only |
| `text` | `.txt`, `.log` | plaintext → summary only |
| `json` | `.json`, `.jsonl`, `.yaml`, `.yml` | plaintext → summary only |

Unsupported extensions fail with `Unsupported format: {ext}. Supported: pdf, png/jpg/gif/webp, docx/xlsx/pptx, md, html, txt, json.`

Apply flag overrides:

- `--raw` → force raw upload regardless of format default (useful when the user wants to preserve a .md file's original bytes).
- `--no-raw` → suppress raw upload regardless of format default (useful when the user considers a PDF disposable and only wants the extracted text).

Record `raw_upload ∈ {true, false}` and `format_kind ∈ {binary, plaintext}` for downstream steps.

### Step 3 — Fetch

Always obtain **extracted text** for the `## Content` section. Additionally, when `raw_upload == true`, obtain a **local file path** pointing at the original bytes for `akb_put_file`.

#### Path source

```text
Read(file_path="{path}")
```

- For PDFs: read page by page when the file is long (`pages="1-20"` at a time). If the PDF's total page count is unknown, attempt without `pages` first; chunk only when the single read fails or exceeds token budget.
- For binary formats `Read` cannot extract (e.g. `.docx`, `.xlsx`), surface the failure: `Cannot extract text from {format}; install a converter or supply an already-converted .md.` The raw bytes can still be uploaded if `raw_upload == true` — ingest proceeds with an empty `## Content` and a note "text extraction unsupported for this format."
- `local_file_path` is the input path itself.

#### URL source

```text
WebFetch(url="{url}", prompt="Return the main article content as clean markdown. Preserve headings, lists, and code blocks. Drop navigation, ads, footers.")
```

If `raw_upload == true`, also download the URL bytes to a temp path (`/tmp/ingest-doc-{short_hash}.{ext}`) for `akb_put_file`:

- Plaintext URLs with `raw_upload=true` (only happens when `--raw` is explicit): save the WebFetch'd HTML/markdown verbatim to temp.
- Binary URLs: fetch bytes directly and save to temp. (If WebFetch does not expose raw bytes, fall back to `Bash(curl -sL -o /tmp/... {url})` — note this in the output if taken.)

Record `extracted_text` (may be empty) and `local_file_path` (may be null).

### Step 4 — Dedup

Two-stage deterministic / semantic check, then a **fully automatic decision table** — no caller flag, no interactive prompt. The skill absorbs the dedup decision (LLM-wiki *agent absorbs bookkeeping*).

Compute `new_hash = sha256(extracted_text)` once up front — used as both the path-source lookup key and the equality test in the decision table.

#### Stage 1 — Deterministic match

Build a `lookup_key`: canonical URL (strip tracking params like `utm_*`, `ref`, `fbclid`) for URL sources, `new_hash` for path sources. Query `akb_search(vault="{vault_name}", query={lookup_key}, collection="corpus-summaries", type="reference", limit=5)`.

For each hit, `akb_get(vault="{vault_name}", doc_id={hit.doc_id})` and inspect the `## Source` block. Match on either a `Location:` line containing the canonical URL **or** a `Content-SHA256:` line matching `new_hash` (only present when a plaintext path was previously ingested). On match, set `dedup_hit = doc_id`, parse `existing_hash` from the existing doc's `Content-SHA256:` (may be absent if the prior ingest had no raw upload), set `dedup_kind = "deterministic"`, and skip Stage 2.

#### Stage 2 — Semantic fallback

Runs only when Stage 1 produced no hit. Produce a provisional title (URL `<title>` tag, PDF metadata, markdown `# heading`, or filename stem — whichever is available). Query `akb_search(vault="{vault_name}", query={provisional_title}, collection="corpus-summaries", type="reference", limit=5)`. Any hit with `score > 0.85` counts as fuzzy: set `dedup_hit = top_hit.doc_id`, `dedup_kind = "fuzzy"`. The hit is a **candidate**, not a confirmed identity match.

#### Decision

| `dedup_hit` / kind | Comparison | Action |
|---|---|---|
| none | — | continue to Step 5 (create path) |
| deterministic | `existing_hash == new_hash` | return `## ingest-doc: unchanged` (see Step 7) and stop. Content identical — no compiled-truth refresh, no timeline append. |
| deterministic | `existing_hash != new_hash` (or `existing_hash` absent) | continue to Step 5; Step 6 uses `akb_update` on `dedup_hit` (compiled-truth refresh + timeline append). |
| fuzzy | — | return `## ingest-doc: likely-exists` and stop. The fuzzy hit is a candidate, not a confirmed identity match — leave disambiguation to the external maintenance layer or human review. |

Edge cases (forced create on a fuzzy false-positive, forced replace despite hash equality) are out of scope for this skill — handle via a separate disambiguation workflow rather than a per-call flag. This keeps the ingest path headless-safe and the skill the sole owner of the dedup decision.

### Step 5 — Upload raw (when applicable)

Skip when `raw_upload == false`. Otherwise: `akb_put_file(vault="{vault_name}", file_path={local_file_path}, collection="corpus-raw", description={provisional_title})`. Capture `file_id` and `s3_key` from the response; compose `Raw URI = akb://{vault_name}/file/{file_id}`. If `local_file_path` was a temp file (URL download), delete it after the upload succeeds: `Bash(rm -f {local_file_path})`.

### Step 6 — Compose and write summary

Compose the summary document body using the fixed five-section structure. The **section headings are fixed**; the **content inside each section adapts to the source**.

**Language**: write TL;DR / Key Points / topic tag descriptors in `--lang` if it was provided; otherwise match the source's primary prose language (not embedded code/quotes). Section headings, frontmatter keys, and the `## Content` zone (mechanical extracted text) always stay in their natural form — headings/keys English, content as-extracted.

#### Body template

```markdown
# {title}

## TL;DR

{Agent-authored 1–3 sentences. What is this document, at a synthesis level. No hedging, no "this document discusses..." boilerplate.}

## Key Points

- {Bullet 1 — the sharpest 3–7 takeaways. For a paper: contribution, method, result. For an article: thesis, argument, conclusion. For a manual: what problem it solves, prerequisites, caveat. For a transcript: decisions, actions, open questions.}
- {Bullet 2}
- ...

## Content

{Cleaned extracted text from Step 3. Preserve original heading hierarchy, code blocks, tables. Drop navigation/ads/footers. For binary sources with no text extraction, write: "Text extraction unsupported for this format. See raw file at {raw_uri}."}

## Source

- Location: {source URL or absolute path}
- Raw: akb://{vault_name}/file/{file_id}     ← omit this line entirely when `raw_upload == false`
- Format: {format (pdf, html, md, …)}
- Content-SHA256: {hash of extracted_text, for future dedup}
- Ingested: {today}

---

## Timeline

- **{today}** | Ingested from {source}. [Source: ingest-doc, {today}]
```

#### Frontmatter

Use only AKB standard fields:

```yaml
type: reference
status: active
title: "{title}"
summary: "{one-sentence synthesis, same voice as TL;DR but compressed to one line}"
tags: ["{format_kind}", "{format}", "topic:{primary}", "topic:{secondary}"]
depends_on: []
related_to: []
implements: []
```

- `format_kind` is `binary` or `plaintext`; `format` is the specific extension (`pdf`, `md`, …). Both are derived from the source.
- `topic:{...}` entries are **1–3 LLM-derived topic tags**, authored alongside TL;DR / Key Points. After synthesizing the body, identify the 1–3 sharpest *subjects* of the document — e.g., a paper on retrieval-augmented generation gets `topic:rag`, `topic:retrieval`. Rules: lowercase, hyphenated for compounds (`topic:vector-search`), prefer domain-anchored vocabulary over generic words (`introduction` / `overview` / `tutorial` / `notes` are not topics), drop a tag rather than reach for filler. Empty topic list is allowed only when the source has no extractable subject (extraction failed, content is a directory listing or single-line note); downstream consolidation will then silently skip the summary.
- All entries are derived from the source — the skill is the sole author of the `tags` array, no caller-supplied tags.
- `depends_on` / `related_to` / `implements` are empty at ingest time — relation-wiring is the external maintenance layer's job.

#### Write

Branch by Step 4 decision.

**New document** (no hit). `akb_put(vault="{vault_name}", collection="corpus-summaries", title={title}, type="reference", tags={tags}, summary={one_sentence_summary}, content={assembled_body})`. Capture `doc_id` and `doc_path`.

**Replace** (deterministic hit with hash differing / absent):

1. `akb_get(vault="{vault_name}", doc_id={dedup_hit}) → existing_content`.
2. Apply timeline merge **Mode B** with `new_entry = "- **{today}** | Re-ingested from {source}. [Source: ingest-doc, {today}]"` and `existing_content` as-is.
3. Build the final `content`: everything from the newly composed body **above** the `---` + `## Timeline` line, then the merged timeline from Mode B. This replaces the compiled-truth zone (TL;DR / Key Points / Content / Source) while preserving all historical timeline entries.
4. `akb_update(vault="{vault_name}", doc_id={dedup_hit}, content={merged_content}, message="Re-ingested from {source}")`.

#### Link raw to doc

When `raw_upload == true` and a doc was created or replaced, materialize the lineage edge: `akb_link(vault="{vault_name}", source="akb://{vault_name}/doc/{doc_path}", target="akb://{vault_name}/file/{file_id}", relation="derived_from")`.

On the `replace` path, first check `akb_relations(vault="{vault_name}", resource_uri="akb://{vault_name}/doc/{doc_path}", type="derived_from", direction="outgoing")` — the raw `file_id` may have changed or the prior ingest may have used `--no-raw`. Skip `akb_link` idempotently when the edge to the new `file_id` already exists. If a `derived_from` edge points to a *different* `file/{id}`, the prior raw is orphaned — do not delete it (other docs may reference it); leave it and emit a warning in the output.

### Step 7 — Report

Emit one of four single-source report blocks based on Step 4 + Step 6 outcome:

```markdown
## ingest-doc: created
- title: {title}
- doc_id: {doc_id}
- path: {doc_path}
- vault: {vault_name}
- collection: corpus-summaries
- raw_file_id: {file_id_or_none}
- summary: {one_sentence_summary}

## ingest-doc: replaced
- title: {title}
- doc_id: {dedup_hit}
- vault: {vault_name}
- previous raw: {old_file_id_or_none}
- new raw: {file_id_or_none}
- timeline entries: {N (after append)}

## ingest-doc: unchanged
- source: {source}
- doc_id: {dedup_hit}
- vault: {vault_name}

## ingest-doc: likely-exists
- source: {source}
- top match: {dedup_hit} (score: {score})
- vault: {vault_name}
```

Glob loop: emit one block per source processed, then a final summary `## ingest-doc: batch complete` with `created` / `replaced` / `unchanged` / `likely-exists` / `failed` counts.

## Failure handling

| Situation | Response |
|---|---|
| `<source>` missing | `Missing required argument: <source>` |
| Path not found | `File not found: {path}` |
| Glob matched zero files | `No files matched "{pattern}".` |
| Unsupported format | `Unsupported format: {ext}. Supported: pdf, png/jpg/gif/webp, docx/xlsx/pptx, md, html, txt, json.` |
| URL unreachable / 4xx / 5xx | `Could not fetch {url}: {status}` |
| Text extraction failed on binary | Record with `## Content` = "Text extraction unsupported for this format. See raw file at {raw_uri}." Not a hard failure when `raw_upload == true`. Hard failure when `raw_upload == false`. |
| AKB write fails | Surface the error verbatim; do not retry. Partially-uploaded raw file is not rolled back at MVP — log `orphaned raw file: {file_id}` in the output so the user can `akb_delete_file` manually. |
| `--vault` not passed | `--vault {name} required.` |
| Vault does not exist | `Vault "{vault_name}" not found. Create with mcp__akb__akb_create_vault first.` |
| AKB MCP server unreachable | `AKB MCP server not accessible. Check your MCP configuration.` |

Do not attempt to create vaults or collections. If the supplied `vault_name` does not exist, `akb_put`/`akb_put_file` will error with a clear message — surface it as-is and instruct the user to create the vault with `mcp__akb__akb_create_vault`.

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
