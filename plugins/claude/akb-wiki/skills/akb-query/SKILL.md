---
name: akb-query
description: Answer a question from the AKB vault — decompose, search (hybrid / graph), ground in compiled-truth-over-timeline precedence, and synthesize a cited answer with gap/conflict/stale flags. Read-only.
argument-hint: '{question} --vault {name}'
allowed-tools: Read, mcp__akb__akb_search, mcp__akb__akb_relations, mcp__akb__akb_graph, mcp__akb__akb_provenance, mcp__akb__akb_get, mcp__akb__akb_drill_down, mcp__akb__akb_browse, mcp__akb__akb_activity
---

# AKB Query

Answer **one question** from the target AKB vault. Decompose the question, search the vault with the strategy that fits, ground the answer in the documents found — honoring AKB's compiled-truth-over-timeline precedence — and synthesize a cited answer. Brain-first: the vault is the source of truth, not the model's prior knowledge.

**Workflow classification.** LLM-wiki **Query** workflow — the **read** half only (à la gbrain's `query`, `mutating: false`). This skill **never writes**: no `akb_put` / `akb_update`, no promote-to-page. Promoting valuable answers into new pages, consolidation, and dream-cycle maintenance are the external maintenance layer's job over the same vault, not this skill's.

Scope boundaries:

- **Read-only.** Every tool call reads. The skill answers from vault content and cites it; it does not modify the vault. (It deliberately does **not** use `akb_grep` — that verb's `replace=` mode rewrites matching documents, so it is not read-only; exact-term lookup goes through `akb_search`, whose hybrid index already includes a keyword/BM25 component.)
- **Brain-first, grounded.** Answer from the vault, never from general knowledge when the vault has relevant content. If the vault lacks the answer, say so — do not fill the gap with a guess.
- **Citations are document-scoped.** AKB exposes provenance at the document level, not per claim, so a claim cites `[doc-uri, section]` — not a sentence-level source.

## Arguments

```text
/akb-query {question} --vault {vault_name} [--collection {path}] [--type {type}] [--since DATE] [--period D1~D2]
```

- `{question}` (required, positional) — a natural-language question. Phrase it as you would ask a person; hybrid search rewards natural phrasing over keyword soup.
- `--vault` (required) — target AKB vault name.
- `--collection {path}` (optional) — scope the search to one collection when the question clearly belongs to it.
- `--type {type}` (optional) — scope to a document type (`decision`, `reference`, `task`, …) when the question implies one.
- `--since DATE` (optional, ISO `YYYY-MM-DD`) — add a freshness axis: cross-check hits against recent vault activity and flag stale ones; surface relevant recent changes.
- `--period D1~D2` (optional, ISO dates) — same as `--since` but bounded to the inclusive range. If both are given, `--period` wins.

If neither `--since` nor `--period` is provided, the freshness axis is skipped entirely.

## Prerequisites

- AKB MCP server is accessible.
- `--vault {name}` provided.
## Workflow

### Phase 1 — Decompose

Parse `--vault`. Hard-fail if missing: `--vault {name} required.` Capture `{question}` and optional flags.

Classify the question into one or more search strategies — a single question may need several:

| Question shape | Strategy | Verb |
|---|---|---|
| Conceptual / open-ended ("how does…", "tell me about…", "why…") **or** exact terms (names, versions, IDs, error strings, code) | hybrid | `akb_search` |
| Relational ("what depends on X", "what implements Y", "how is A connected to B") | graph | `akb_relations` / `akb_graph` |

Pick the minimum set that covers the question. Most questions are hybrid — `akb_search` is vector + keyword/BM25, so it handles both conceptual phrasing and exact tokens; pass the exact token as the query when the question pins one. Reach for graph only when the question asks about links between documents.

### Phase 2 — Search

Issue every selected strategy in a **single parallel tool-use block** — they are independent. Run only the strategies Phase 1 chose. **Exception:** when the relational strategy is selected but no anchor URI is known yet, that strategy is *not* independent — run the hybrid search first to find the anchor, then issue `akb_relations` / `akb_graph` in a follow-up block. Never call them with a guessed URI.

- **Hybrid** — `akb_search(query="{question}", vault="{vault_name}", limit=10)`. For an exact token, pass the token itself as the query — the hybrid index matches it on the keyword/BM25 side. Add `collection=` / `type=` from the flags or when the question clearly implies one. Add `tags=[…]` only if the question names specific tags.
- **Relational** — when an anchor document is known, `akb_relations(uri="{anchor_uri}", direction=…, type=…)` for direction- and type-filtered edges (single-hop). For a multi-hop neighborhood, `akb_graph(uri="{anchor_uri}", depth=…)` (BFS; no direction filter — it returns all edges at each level). If the anchor is not yet known, find it with a hybrid search first.
- **Freshness (conditional)** — only when `--since` / `--period` was given: `akb_activity(vault="{vault_name}", since="{start_date}", limit=20)`. For `--period D1~D2`, pass `since=D1` and drop entries after `D2` in Phase 3.

Dedup the combined hits by document URI before Phase 3.

### Phase 3 — Ground with source precedence

For the top 3–5 deduped hits, read enough to answer — cheapest read first:

1. `akb_drill_down(uri="{uri}", mode="outline")` to see the document's structure.
2. Read the **compiled-truth (top) sections first** via `akb_drill_down(uri, section="…")` — that is the document's current best understanding. Treat the **timeline (bottom) section** as supporting evidence, not the headline.
3. `akb_get(uri)` only when you need the full document.
4. `akb_provenance(uri)` when the question is about authorship or recency ("who decided…", "when was…") — document-level who/when.

**Source precedence when documents conflict** (highest authority first):

1. The user's direct statements in this conversation.
2. Compiled truth (top of a document).
3. Timeline entries (bottom of a document).
4. External / general knowledge (lowest — and only when the vault is silent).

**Staleness** — if a freshness axis was requested, cross-check each hit against `akb_activity`: mark a hit `[stale]` when it has not changed within the window, and drop activity entries past a `--period` upper bound.

### Phase 4 — Synthesize & answer

Write a direct answer to the question, grounded in what Phase 3 read:

- **Cite every claim** as `[doc-uri, section]`. When a claim rests on several documents, cite all of them.
- **Conflicts** — when sources disagree, present both with their citations and name the contradiction. Never silently pick one.
- **Gaps** — if the vault does not answer the question, say so plainly (`vault "{vault_name}"에 X에 대한 정보가 없습니다`) instead of answering from general knowledge. Offer to broaden the search or browse the vault.
- **Staleness** — surface `[stale]` markers next to claims drawn from stale documents.

Response shape:

```markdown
## {question}

{Direct answer in prose, with inline citations: "According to [research/vector-index, compiled truth], …"}

{If sources conflict: a short "Conflicting sources" note citing both.}

{If a freshness axis was requested and recent changes are relevant:}
### Recent changes ({period label})
- [{doc_uri}] {title} — updated {updated_at}

{If the vault is silent on part of the question:}
### Gaps
- The vault has no information on {X}.

---
*{N} documents consulted in vault "{vault_name}"*
```

Render `{period label}` as `since YYYY-MM-DD` or `YYYY-MM-DD~YYYY-MM-DD`.

## Error Handling

| Situation | Response |
|---|---|
| AKB MCP not accessible | `AKB MCP server not accessible. Check your MCP configuration.` |
| `--vault` not passed | `--vault {name} required.` |
| Vault not found | `Vault "{vault_name}" not found. Create with mcp__akb__akb_create_vault first.` |
| No hits across every strategy | Flag the gap explicitly, then offer to broaden terms, drop `--collection`/`--type`, or browse with `akb_browse`. |
| Relational anchor URI unknown | Find the anchor with a hybrid search first; if none is found, answer from hybrid hits and note the missing anchor. |
| `akb_activity` fails | Drop the freshness axis only; answer from the search hits and note the skipped staleness check. |
| `--since` / `--period` value is not ISO | Ask the user to restate the date (`YYYY-MM-DD`) before running. |

## Tips

- Natural-language questions beat keyword strings — "why did we drop the per-plugin config" beats "config".
- Compiled truth (top of a document) is the answer; the timeline (bottom) is the evidence. Lead with the former.
- For relational questions, name the anchor document — graph traversal needs a starting URI.
- Use `akb_browse` to explore vault structure when a search returns nothing and you are unsure what exists.
- A temporal cue ("what changed since the release", "recent decisions") is what `--since` / `--period` is for — it adds the staleness axis hybrid search alone misses.
