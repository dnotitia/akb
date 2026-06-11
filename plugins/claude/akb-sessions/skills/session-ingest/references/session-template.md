# Session Note Template

The session note is a navigator + narrative + timeline. Deep knowledge (reusable lessons, long-lived decisions, follow-up tasks, creative ideas) belongs to the sub-notes drafted in Phase 3 and must not be restated in the session body.

## Draft format

```markdown
---DRAFT---
title: {brief description}
collection: sessions
type: session
tags: [session, claude-code, session, session:{session_id}, last-synced:{jsonl_last_timestamp}, project:{project_name}, {topic-specific tags}]
project: {project name from working directory or JSONL context}
repo_url: {repo_url from Phase 2-2, or omit if null}
status: active
summary: {1-2 sentence summary of the session}

# {Brief Description}

> **Summary:** {2-3 sentence overview of what the session accomplished and why it matters}

## Narrative

{Chronological prose, 1–2 paragraphs: event → diagnosis → resolution → outcome. No bullets. If the session had a Problem/Solution arc, weave it into one narrative paragraph. Do not generalize reusable lessons here — promote them to TIL instead. The session records "what happened"; sub-notes record "what we learned."}

## Artifacts

- **Commits**: `{hash}` — {one-line subject}
- **Files touched**: `{path/to/file}` — {why it matters}
- **External sources**:
  - [{Title}]({url}) — {relevance}

## Knowledge Produced

<!-- Phase 4-0 populates this block from sub-note drafts. Leave as a placeholder here. -->

## Open Questions

- **Q1**: {Unresolved question 1}
- **Q2**: {Unresolved question 2}

---

## Timeline

- **{today YYYY-MM-DD}** | {For new mode: Session captured. {one-line summary}. | For update mode: Session resumed. {one-line summary of new work}.} [Source: session:{session_id}, {today YYYY-MM-DD}]
---END_DRAFT---
```

`## Open Questions` bullets must be numbered `Q1`, `Q2`, … so that Phase 3 sub-drafters can target a specific question via `resolves_question: Q{n}`. Phase 4-0 reconciles those references.

## Section omission rules

These apply to the **final** session body after Phase 4-0, not the draft itself:

- `## Narrative` is always present (at least one sentence).
- `## Artifacts` is present if any commits, files, or external URLs were touched.
- `## Knowledge Produced` is populated by Phase 4-0 from sub-note results; omitted entirely if no sub-notes produced.
- `## Open Questions` is omitted if every question was promoted in Phase 4-0 or there were none.

Every session draft **must** end with the `---` separator and a `## Timeline` section containing exactly **one** new entry for this ingest. Do not re-include past timeline entries — Phase 4-2 merges your new entry with the existing timeline.

## Update mode

Read `existing_session_content` (stored in Phase 1-2) to understand what the original session produced. Write the compiled-truth sections (Summary, Narrative, Artifacts, Knowledge Produced placeholder, Open Questions) as the **combined** current state — integrating both the original work and the new work performed after `last_synced_timestamp`.

- Title can stay as-is unless the new work fundamentally changes the session's theme.
- Summary should read as a unified description of the full session, not only the delta.
- The single new timeline entry describes what this sync added.
- Open Questions from the previous sync that remain unresolved keep their original `Q{n}` IDs; new questions continue numbering.

## Authoring guidelines

- Write as if the reader is you, six months from now, trying to remember what happened.
- Prefer concrete details (file names, function names, error messages) over vague descriptions in `## Narrative`.
- **External URLs**: link inline where mentioned in Narrative, and collect all referenced URLs in `## Artifacts > External sources` (the session note is the canonical bibliography). Omit transient URLs (localhost, CI build links).
- Sub-notes do **not** maintain their own `## Sources` section — the session Artifacts covers them.
- If the session was trivial, produce a short note rather than padding content; skip sections that have nothing meaningful to say.
- Write prose content in the primary language of the user's prose in the JSONL conversation (not embedded code/quotes); metadata keys and section headings remain in English.
- Do **not** draft `## Knowledge Produced` content here — leave the placeholder and let Phase 4-0 fill it.
