# Note Templates Reference

Templates for each note type produced by the `/session-ingest` workflow. Notes are stored as plain standard markdown via AKB MCP tools. All metadata is passed through `akb_put` parameters — no YAML frontmatter in the content body.

## Body Structure: Compiled Truth + Timeline

Every note body follows the same two-zone layout, separated by a horizontal rule:

```markdown
# {Title}

{Compiled truth sections — the current best synthesis. Rewritten as understanding evolves.}

---

## Timeline

- **YYYY-MM-DD** | {One-line summary of the event.} [Source: {attribution}]
  - {Optional sub-bullet for detail.}
- **YYYY-MM-DD** | {Earlier event.} [Source: {attribution}]
```

**Rules:**

- **Above the line (compiled truth):** the note's current state — sections like `## Context`, `## What I Learned`, `## Core Idea`, etc. Rewrite these when new information changes the picture. Read just this part and you have the full current answer.
- **Below the line (timeline):** append-only log of events that shaped the note, newest first. Never edit or delete past entries. Every compiled-truth change should leave a timeline entry explaining what and why.
- **Always include the `---` separator and `## Timeline` heading**, even when the timeline has a single entry (the initial capture).
- **Order timeline entries newest-first**, so the most recent event is at the top.
- **Each timeline entry cites a source** in `[Source: ...]` form (see below).

### Session Notes: Navigator Role

Session notes use the same two-zone layout but play a **navigator + narrative** role rather than a full knowledge store. The compiled-truth zone tells the story of what happened and points to the sub-notes (TIL / Task / Idea / Decision) that hold the reusable knowledge. The session note itself should remain readable as a standalone log of the session — but deep content lives in sub-notes, linked from the `## Knowledge Produced` block.

### Source Attribution

Every timeline entry needs a citation. Use one of:

| Event source | Attribution format |
|---|---|
| Session ingest (initial capture or append) | `[Source: session:{session_id}, {YYYY-MM-DD}]` |
| Manual edit by user | `[Source: manual, {YYYY-MM-DD}]` |

### When to Rewrite Compiled Truth vs. Append-Only

- **New information that replaces or refines existing content** → rewrite the relevant compiled-truth section, then append a timeline entry describing the change.
- **New information that adds without conflicting** → extend the compiled-truth section (e.g., add a new gotcha to `## Gotchas`), then append a timeline entry.
- **Status or metadata changes** (task resolved, doc superseded, cross-link added) → timeline entry only; compiled truth stays as a historical record of the state at the time the note was last written.

## Metadata → akb_put Parameter Mapping

| Draft Field | akb_put Parameter | Notes |
|-------------|-------------------|-------|
| title | `title` | Direct mapping |
| collection | `collection` | Target collection |
| type | `type` | AKB document type enum |
| tags | `tags` | Array of strings |
| summary | `summary` | Agent-generated summary |
| (session_doc_id) | `depends_on` | Set at write time for sub-notes |
| (reviewer related) | `related_to` | Set at write time from reviewer output |

## Type Mapping

| Note Type | akb_put type | Collection | Notes |
|-----------|-------------|------------|-------|
| Session | `session` | `sessions` | One per ingest run — navigator + narrative + timeline |
| Learning (TIL) | `reference` | `sessions/learnings` | Reusable knowledge. Classify with `til_kind:*` tag |
| Task | `task` | `sessions/tasks` | Follow-up work |
| Idea | `plan` | `sessions/ideas` | Forward-looking concepts |
| Decision | `decision` | `sessions/decisions` | Long-lived architectural/product decisions (ADR style). Holds `## Alternatives Considered` as the canonical home for anti-patterns |

## Tag Conventions

- `session` — on every generated note
- `claude-code` — provenance tag for the runtime
- `session`, `learning`, `task`, `idea`, `decision` — type tags
- `project:{name}` — project namespace
- `{technology}` — e.g. `python`, `react`, `sqlite`
- `P0` through `P3` — priority tags on tasks
- `{category}` — idea category tags (architecture, product, workflow, exploration)
- `til_kind:shift` | `til_kind:acquisition` | `til_kind:comparison` | `til_kind:troubleshooting` — TIL classification (exactly one per TIL)
- `session:{session_id}` — links the note to its originating JSONL session (session notes only)
- `last-synced:{ISO-timestamp}` — records the last JSONL timestamp covered by this ingest (session notes only)
- `supersedes:{doc_id}` — on a new decision that replaces an older one
- `superseded-by:{doc_id}` — on a decision that has been replaced
- `resolved` — marks tasks that have been completed
- `idea_status:promoted` — marks ideas that spawned a decision or task (applied by the external maintenance layer)
- `concept` — marks consolidated knowledge pages (created by the external maintenance layer)
- `auto-generated` — marks documents created by automated processes (concept pages)

## External URLs

External URL preservation differs between session notes and sub-notes.

### Session Note — Canonical Bibliography

The session note's `## Artifacts > External sources` block is the **canonical bibliography** for the ingest. Every external URL referenced in the session is collected here, regardless of which sub-note it ended up in. This gives a single, searchable list per session.

```markdown
## Artifacts

- **External sources**:
  - [Title](https://example.com) — why it's relevant
  - [Another](https://example.org) — context
```

### Sub-note — Inline Links Only

TIL / Task / Idea / Decision bodies reference external URLs only as **inline markdown links** where they are mentioned in the body text. Sub-notes do **not** maintain their own `## Sources` section — the session note's Artifacts block holds the canonical list, and each sub-note points back to its session via `depends_on`.

```markdown
## What I Learned

[Claude Code](https://github.com/anthropics/claude-code)의 컨텍스트 윈도우 한계 근처에서
발화자 혼동 버그가 발생한다는 [분석 글](https://dwyer.co.za/static/claude-mixes-up...)이 있다.
```

**Include (in session Artifacts):** URLs that directly support or provide context for the session's content — articles cited, repos explored, documentation referenced, tools discovered.

**Exclude:** Transient URLs (localhost, CI build links, temporary file shares) and URLs the user merely passed through without engagement.

## Cross-Note Links

AKB does **not** support Obsidian-style `[[wikilink]]` syntax. Use standard markdown links only.

Sub-notes are referenced from the session note's `## Knowledge Produced` block using plain markdown:

```markdown
- 🧠 TIL: [Title](sessions%2Flearnings%2Ftitle-path.md) — one-line hook
```

The AKB backend automatically extracts these body links into `links_to` graph relations, and each sub-note's `depends_on=[session_doc_id]` frontmatter parameter sets the reverse relation. The hybrid (inline link + frontmatter relation) is the canonical pattern.

## Highlighted Text Patterns

Standard markdown replacements for Obsidian callouts:

| Purpose | Format |
|---------|--------|
| Session overview | `> **Summary:** {text}` |
| Key insight | `> **Key Insight:** {text}` |
| Next action | `> **Next Action:** {text}` |
| Inspiration source | `> **Inspiration:** {text}` |
| Decision context | `> **Context:** {text}` |

## Knowledge Produced Block

Used in the session note to point at sub-notes written during this ingest. Each line uses a type icon, a markdown link to the sub-note path, and a one-line hook:

```markdown
## Knowledge Produced

- 🧠 TIL: [TIL 학습 신호 확장](sessions%2Flearnings%2Ftil-학습-신호-확장.md) — 놀라움 + 지식 획득 둘 다 포착해야 함
- 💡 Idea: [테스트 실행 레이어 분리](sessions%2Fideas%2F테스트-실행-레이어-분리.md) — 빠른/느린 검증을 명시적으로 나누기
- ✅ Task: [Tycho 업그레이드 재검증](sessions%2Ftasks%2Ftycho-업그레이드-재검증.md) — JUnit 5 병렬 실행 지원 여부
- 📐 Decision: [JUnit 5 병렬 실행 미도입](sessions%2Fdecisions%2Fjunit-5-병렬-미도입.md) — 현 Tycho 버전에서 실효성 없음
- ♻️ Resolved: [이전 태스크 제목](sessions%2Ftasks%2Fresolved-task-path.md) — 이번 세션에서 해결됨
```

Icons (use exactly these):

| Icon | Purpose |
|------|---------|
| 🧠 | TIL (newly created or appended learning) |
| 💡 | Idea (newly created or refined idea) |
| ✅ | Task (newly created or updated task) |
| 📐 | Decision (newly created or appended decision) |
| ♻️ | Resolved task (existing task closed in this session) |

Omit the `## Knowledge Produced` section if no sub-notes were produced. Do not create sub-section headers within the block — a single flat bullet list is the entire block.

## Session Note Template

**akb_put**: `type="session"`, `collection="sessions"`

```markdown
# {Brief Description}

> **Summary:** {2-3 sentence overview of what the session accomplished and why it matters}

## Narrative

{Chronological prose, 1–2 paragraphs: event → diagnosis → resolution → outcome. No bullets; defer mechanical facts to Artifacts. If the session had a Problem/Solution arc, weave it into one narrative paragraph. Do not generalize reusable lessons here — promote them to TIL (e.g. `til_kind:troubleshooting`).}

## Artifacts

- **Commits**: `{hash}` — {one-line subject}
- **Files touched**: `{path/to/file}` — {why it matters}
- **External sources**:
  - [{Title}]({url}) — {relevance}

## Knowledge Produced

- 🧠 TIL: [{title}](sessions%2Flearnings%2F{path}.md) — {one-line hook}
- 💡 Idea: [{title}](sessions%2Fideas%2F{path}.md) — {one-line hook}
- ✅ Task: [{title}](sessions%2Ftasks%2F{path}.md) — {one-line hook}
- 📐 Decision: [{title}](sessions%2Fdecisions%2F{path}.md) — {one-line hook}
- ♻️ Resolved: [{title}](sessions%2Ftasks%2F{path}.md) — {how resolved}

## Open Questions

- {Unresolved question that did NOT get promoted to an Idea/Task/Decision}

---

## Timeline

- **{YYYY-MM-DD}** | Session captured. {One-line summary of work performed.} [Source: session:{session_id}, {YYYY-MM-DD}]
```

**Section omission rules:**

- `## Narrative` is always present (at least one sentence).
- `## Artifacts` is present if any commits, files, or external URLs were touched. Omit sub-bullets with no entries.
- `## Knowledge Produced` is omitted entirely if no sub-notes were produced.
- `## Open Questions` is omitted if empty (every question was promoted to a sub-note, or there were none).

**Update mode:** rewrite the compiled-truth sections (Summary / Narrative / Artifacts / Knowledge Produced / Open Questions) to reflect the combined session (original + continuation). Knowledge Produced should include every sub-note from both the original ingest and the resumed portion. Append a new timeline entry, e.g. `- **{YYYY-MM-DD}** | Session resumed. {new work summary}. [Source: session:{session_id}, {YYYY-MM-DD}]`. Never drop past timeline entries.

## Learning Note (TIL) Template

**akb_put**: `type="reference"`, `collection="sessions/learnings"`, `depends_on=[session_doc_id]`, tag one of `til_kind:shift|til_kind:acquisition|til_kind:comparison|til_kind:troubleshooting`.

```markdown
# {Concise Learning Title}

> **Key Insight:** {One sentence takeaway}

## Context

{When and why this came up}

## What I Learned

{Technical explanation with enough detail to be actionable}

```{language}
{Code example demonstrating the concept}
```

## Gotchas

- {Pitfall or edge case to watch for}

---

## Timeline

- **{YYYY-MM-DD}** | Captured from session:{session_id}. {One-line summary of the lesson.} [Source: session:{session_id}, {YYYY-MM-DD}]
```

**til_kind classification** (exactly one tag per TIL):

| Tag | When to use |
|-----|-------------|
| `til_kind:shift` | Mental model correction — user expected X but learned Y |
| `til_kind:acquisition` | Knowledge acquisition — user entered without knowledge and built understanding through exploration |
| `til_kind:comparison` | Structured comparison/analysis — A vs B tradeoffs the user evaluated |
| `til_kind:troubleshooting` | Diagnostic/root-cause pattern — non-obvious root cause traced through systematic investigation |

**Optional sections by til_kind:**

- `til_kind:comparison` may include a `## Comparison` table section
- `til_kind:troubleshooting` may include a `## Root Cause` section describing the diagnostic path

## Task Note Template

**akb_put**: `type="task"`, `collection="sessions/tasks"`, `depends_on=[session_doc_id]`

```markdown
# {Descriptive Title}

> **Next Action:** {Specific first step}

## Context

{Background and motivation — why this task exists}

## Steps

- [ ] {Step 1}
- [ ] {Step 2}
- [ ] {Step 3}

## Related Files

- `{path/to/file}` — {why it's relevant to resuming this task}

## Notes

{Task-handling nuance: scheduling constraints, blocking/non-blocking status, links to background}

---

## Timeline

- **{YYYY-MM-DD}** | Task created from session:{session_id}. [Source: session:{session_id}, {YYYY-MM-DD}]
```

Task lifecycle events that append to the timeline:

- `- **{date}** | Task updated in session:{id}. {what changed}. [Source: session:{id}, {date}]`
- `- **{date}** | Task resolved in session:{id}. {how it was addressed}. [Source: session:{id}, {date}]`

## Idea Note Template

**akb_put**: `type="plan"`, `collection="sessions/ideas"`, `depends_on=[session_doc_id]`

Idea notes read as lightweight PRDs / technical proposals. The body must contain enough substance to judge whether the idea is worth pursuing — not just describe what it is.

**Assessment frontmatter.** In addition to the standard fields, every idea note carries three enum assessments set by the drafter:

- `impact: high | medium | low` — expected benefit magnitude.
- `effort: small | medium | large` — rough implementation size.
- `confidence: high | medium | low` — how confident the Proposal actually solves the Problem.

These values drive the main ingest skill's Phase 3-2 triage. The drafter silent-drops any idea it rates `impact: low` before emitting a draft; the main agent filters further (`impact: low`, or `effort: large AND confidence: low`).

```markdown
# {Compelling Title}

> **Inspiration:** {What triggered this idea — cite specific files, patterns, or workarounds from the session}

## Problem

{Reproducible observation. What was harder than it should be, how often it recurred, who is affected. Concrete evidence beats "felt awkward".}

## Proposal

{Design-level solution. What changes in code, architecture, or flow — specific enough for someone to begin an implementation sketch.}

## System Architecture

{Optional. When the idea spans multiple components, show how they fit: a compact diagram (mermaid / ASCII) or a labeled bullet list of components and data flows.}

## Expected Impact

{First line is the rationale for the `impact` frontmatter value. Then: who benefits, how much, and by what measure. Prefer quantifiable statements ("turns N retries per sync into 0") over qualitative ones.}

## Effort & Risks

{Rough implementation surface (affected modules / files at directory granularity), top 1–3 risks, and any assumption that could invalidate the Proposal.}

## Success Criteria

{Observable or measurable signals that the idea is working after it ships. Each criterion should be checkable without rerunning the original session.}

## Open Questions

- {Key uncertainty}

---

## Timeline

- **{YYYY-MM-DD}** | Idea captured from session:{session_id}. [Source: session:{session_id}, {YYYY-MM-DD}]
```

**Section omission rules:**

- `Problem`, `Proposal`, `Expected Impact`, `Effort & Risks`, `Success Criteria` are always present.
- `System Architecture` is optional — include it when the idea spans multiple components; omit it for small local changes.
- `Open Questions` is omitted when there are no unresolved uncertainties.

**Idea lifecycle events** that append to the timeline:

- `- **{date}** | Idea refined in session:{id}. {what changed}. [Source: session:{id}, {date}]` — when a later session revises Proposal / Expected Impact / etc.

The external maintenance layer may later set `idea_status:promoted` on an idea that spawned a decision or task. `idea_status:*` tags are append-preserving — session-ingest Phase 4-3 merges existing tags with new draft tags on `disposition: append`, so a promoted idea keeps its status tag through subsequent refinements.

## Decision Note Template

**akb_put**: `type="decision"`, `collection="sessions/decisions"`, `depends_on=[session_doc_id]`

Follows the ADR (Architecture Decision Record) pattern. A decision note is the canonical home for a chosen direction plus every alternative that was considered. Anti-pattern memory — "what we tried, why it doesn't work" — lives here, not in the session note.

```markdown
# {Decision Title}

> **Context:** {Why this decision was needed — what forced the choice}

## Decision

{What was chosen, in one tight paragraph. State the decision first, reasoning second.}

## Consequences

{What this decision enables and constrains. Include both positive and negative outcomes.}

## Alternatives Considered

### {Alternative Approach Title}

**What**: {The approach — specific enough to be recognizable if revisited}
**Why not**: {Why it was rejected. Include concrete evidence — paths tried, error messages, measurements — that would save someone from repeating the experiment}

### {Another Alternative}

**What**: {…}
**Why not**: {…}

---

## Timeline

- **{YYYY-MM-DD}** | Decision captured from session:{session_id}. [Source: session:{session_id}, {YYYY-MM-DD}]
```

**Section omission rules:**

- `## Context`, `## Decision`, `## Consequences`, `## Alternatives Considered` are always present.
- Supersede relationships are tracked via frontmatter `supersedes:` / `superseded-by:` tags and a Timeline entry — not a body section.

**Decision lifecycle events** that append to the timeline:

- `- **{date}** | Decision refined in session:{id}. {what changed in Consequences or Alternatives}. [Source: session:{id}, {date}]`
- `- **{date}** | Superseded by {newer_title} ({newer_doc_id}). {reason}. [Source: session:{id}, {date}]` — paired with setting tag `superseded-by:{newer_doc_id}` on this note and `supersedes:{this_doc_id}` on the new note.

**When to write a Decision note** (vs. keep in session Narrative):

- The choice has **long life** — likely to be referenced from future sessions.
- The choice **constrains future work** — architecture, API contract, tooling, process.
- A **real alternative** was evaluated and rejected, not a trivial preference.

One-off tactical choices that only matter inside the session (e.g., "used `sed` instead of `awk` for this one-liner") stay in Narrative prose.
