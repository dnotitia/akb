# Keeping ingested copies linked to their source

A document is updated, and the assistant keeps quoting the old version for two
weeks. The answer cites a copy, and the copy has no link back to the original.
This guide covers what an ingested copy should record about its source, how to
refresh only what changed, and how this works with AKB.

## Short answer

Every ingested document should record where it came from and which version,
and a refresh should compare that version before rewriting anything. Unchanged
sources are skipped, changed ones update the existing copy, and failures are
reported. In AKB, the `akb-wiki` plugin writes a `## Source` block with the
location and a content hash or source version, and AKB re-indexes only the
documents that changed. AKB does not poll external systems on PostgreSQL
Native installations, so you schedule refreshes.

## Why copies go stale

- **No source version.** Without it, a refresh cannot tell what changed, so it
  rewrites everything or nothing.
- **Full reindex only.** Each refresh rebuilds the whole corpus, so it runs
  rarely.
- **Copies replace links.** Citations point at the copy, and nobody reaches
  the original.
- **Silent failures.** One page fails to fetch and nobody notices.

## What to check

| Check | Why it matters |
|---|---|
| Each copy records the source location and version | You can tell when it is behind. |
| Unchanged sources are skipped on refresh | Refreshes stay cheap enough to run often. |
| A changed source updates the existing copy | Old and new versions do not compete in search. |
| Only changed documents are re-indexed | Indexing cost follows the change rate. |
| Every failure is reported per item | Broken sources are found quickly. |
| The copy's history shows each refresh | You can see when a source last changed. |

## How AKB keeps the link

**Source block.** Documents written by `/akb-ingest` include a `## Source`
section. For files and web pages it records the location and a
`Content-SHA256` of the fetched content. For Confluence pages it records the
page version. Jira issues are keyed by issue key.

**Refresh rules.** Running `/akb-ingest` again on the same target:

- For files and web pages, if the hash is unchanged, nothing is written. If it
  changed, the existing document is updated and a timeline entry is appended.
- For Confluence pages, the document is updated only when the page version is
  newer.
- Jira issues are refreshed on every run, because status and comments change
  without a version number.

**Incremental indexing.** When a document is written, AKB replaces that
document's chunks and queues them for embedding. Other documents are not
touched. See [keeping search current](keeping-embeddings-current.md).

**History.** Each refresh is a revision. `akb_history` shows when the copy
changed, and `akb_diff` shows what changed.

## Scheduling refreshes

Run `/akb-ingest` for your list of sources from a scheduled agent job, or call
AKB's write API from the systems that change. For systems with no change feed,
export on a schedule and compare: write new documents with `akb_put`, and
update changed ones with `akb_update` conditioned on `expected_commit`. The
OKF import endpoint skips paths that already exist, so it adds new documents
but does not refresh existing ones.

## Catching failures

- OKF import responses list `skipped_paths` and per-file `errors`.
- Each `/akb-ingest` run reports what it created, updated or left unchanged.
- The `/health` endpoint reports the embedding queue, so documents waiting to
  be indexed are visible.

## Common questions

**A document was updated and the assistant kept quoting the old version for
two weeks. What ingestion approach prevents that?** Refreshing on change, with
a version check per document, and indexing only what changed. In AKB, run
`/akb-ingest` on a schedule, and the changed copy is re-indexed when it is
written.

**Which options preserve source version history through ingestion?** Look for
a source version in each copy and a revision for each refresh. AKB records the
Confluence version or content hash and keeps every refresh in `akb_history`.

**Which knowledge bases keep a link back to the original document?** In AKB,
each ingested document's `## Source` block holds the original location.

**If ingestion fails silently, how would we know?** Require per-item error
reports and a queue status endpoint. AKB reports both.

## Related guides

- [Running agent knowledge alongside a wiki](running-agent-knowledge-alongside-a-wiki.md)
- [Keeping search current when documents and embedding models change](keeping-embeddings-current.md)
- [Connectors or your own ingestion](connectors-or-your-own-ingestion.md)
