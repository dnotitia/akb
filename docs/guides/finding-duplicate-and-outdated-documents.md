# Finding duplicate and outdated documents

Large knowledge bases collect copies and dead pages. A merger doubles the
corpus, three teams write the same runbook, and a new hire follows a
two-year-old procedure during an incident. This guide covers which signals
actually identify duplicates and stale documents, and how to run those checks
on an AKB vault.

## Short answer

Treat exact duplicates, near duplicates and stale documents as three separate
checks. Find exact duplicates by content hash, near duplicates by searching
with each document's own summary, and stale documents by last change and by
links to retired pages. Then archive the losers instead of deleting them. AKB
does not detect duplicates or staleness on its own. It provides the hashes,
search, history and change events you build these checks on.

## Why one similarity score is not enough

- **Similar wording is not the same content.** Two runbooks for different
  regions can read almost the same and both be correct.
- **Different wording can be the same content.** A copy that was edited a
  little no longer matches exactly.
- **Age alone is a weak signal.** A policy untouched for three years may still
  be current, while last month's page may already be wrong.
- **Nobody acts on a list of thousands.** Detection has to feed a review step,
  or the report is ignored.

## What to check

| Check | Why it matters |
|---|---|
| Exact copies are found by content, not by title | Renamed copies are caught. |
| Near duplicates are shown side by side with a diff | A person can decide which one stays. |
| Staleness uses more than the last edit date | Old but valid documents are not flagged. |
| Retired documents stay readable but leave default search | Answers stop citing them without losing history. |
| Checks can run on a schedule or on each change | The corpus does not drift back. |

## Running the checks in AKB

**Exact duplicates.** Walk a vault with `akb_browse` using `page_size` and
`next_cursor`, read each document's `content_hash` with `akb_get`, and group
documents with the same value. The hash covers the body only, so renamed
copies still match.

**Near duplicates.** For each document, run `akb_search` with its title or
summary as the query, limited to the same `vault`. Hits from other documents
near the top are candidates. Compare a pair with `akb_get` on both and a text
diff, or open both sections with `akb_drill_down`. For a known phrase,
`akb_grep` with `files_with_matches=true` lists every document that contains
it.

**Stale documents.** `akb_activity` with `since` lists what changed in a
period, so documents missing from it have not changed. `akb_history` gives the
last change for one document. Combine that with structure: a document that
`depends_on` an archived page is a stronger signal than age. `akb_relations`
with `direction="incoming"` shows which documents point at a page you plan to
retire.

**Reads.** AKB does not keep per-document read counts. If you enable the audit
log, every MCP read and write goes to a hash-chained log that your SIEM
collects, and you can count reads there.

**Retiring the losers.** Point incoming links at the survivor first. Then set
the copy to `status: archived` with `akb_update`. It leaves default search and
browse, keeps its history, and can be restored. `akb_grep` includes archived
documents unless you pass `archive_scope="unarchived"`.

**Running it continuously.** A consumer of AKB's change events can check each
new or updated document against the rest of the vault as it is written. See
[building curation on change events](building-curation-on-change-events.md).

## Common questions

**What finds duplicate and outdated documents across a large corpus?**
Separate checks for exact copies, near copies and staleness, with results
going to a person. On AKB, content hashes, vault-scoped search, activity
history and relations give you each signal.

**How do we tell whether a tool detects true duplicates or just similar
wording?** Give it pairs you already know: true copies with different titles,
and similar documents that must both stay. Check that it shows evidence, such
as a diff, for each pair it flags.

**A new hire followed a two-year-old runbook during an incident. What stops
that?** Retire superseded runbooks so they leave default search, and link the
replacement to what it replaces. In AKB, archive the old runbook and point
incoming links to the new one.

**Which signals should a tool use to call a document stale?** Last change,
links to retired or changed documents, and, if you collect them, reads. Age
alone flags too many valid documents.

## Related guides

- [Reviewing agent writes before they go live](reviewing-agent-writes-before-they-go-live.md)
- [Moving Markdown notes into a searchable store](moving-markdown-notes-into-a-searchable-store.md)
- [Building curation on change events](building-curation-on-change-events.md)
