# Citing the exact source behind an answer

An answer is only as useful as a reviewer's ability to check it. Many
assistants cite a document title, so reviewers still reread the whole page,
and some keep quoting text that was changed weeks ago. This guide covers what
a usable citation contains and how to produce one from AKB.

## Short answer

Cite the document, the section and the revision. A reviewer should be able to
open the exact passage the answer used and see whether the document has
changed since. In AKB, every document has a stable `akb://` URI,
`akb_drill_down` returns a single section, `akb_get` returns the current
commit, and `akb_get` with `version` returns the text at an older commit.
AKB's citations are document and section level. It does not attach a source to
each sentence.

## Why citations fail reviewers

- **Title-only citations.** The reviewer still has to find the passage.
- **Copies instead of originals.** The link opens an export, not the document
  people maintain.
- **No revision.** Nobody can tell whether the answer used today's text or
  last quarter's.
- **Stale index.** The document was edited, but search still returns the old
  chunks.

## What to check

| Check | Why it matters |
|---|---|
| Each citation identifies a section, not only a document | Reviewers open the passage directly. |
| Citations can name the revision that was read | You can tell if the source changed later. |
| Links point to the maintained document | Fixes reach the cited source. |
| Search reflects an edit soon after it is saved | Answers stop quoting replaced text. |
| Authorship and history are one call away | Reviewers can see who wrote it and when. |

## Building citations on AKB

**Identify the document.** Every search hit carries a canonical `uri`, such as
`akb://handbook/coll/policies/doc/travel.md`. The URI stays the same across
edits, and `akb_move` keeps an old URI resolving after a rename.

**Identify the section.** `akb_drill_down` with `mode="outline"` lists a
document's headings. With `section`, it returns only the matching section's
text. Cite the URI with the heading, for example
`[akb://handbook/coll/policies/doc/travel.md, Approval limits]`. The
`akb-query` skill in the `akb-wiki` plugin answers this way and flags
conflicts between sources.

**Identify the revision.** `akb_get` returns `current_commit`. Store it with
the citation. Later, `akb_history` shows whether newer commits exist,
`akb_diff` shows what changed, and `akb_get` with `version` returns the text
the answer was based on.

**Show who wrote it.** `akb_provenance` returns who created a document, when,
and its relations in the vault.

**Keep search in step with edits.** On a write, AKB replaces the document's
chunks and queues them for embedding. In PostgreSQL Native installations,
exact search with `akb_grep` reads stored text, so it finds the new wording
before embeddings are ready. See
[keeping search current](keeping-embeddings-current.md).

**Retire old versions explicitly.** When a policy is replaced, archive the old
document so it leaves default search. Citations to it still resolve, so
reviewers can see that the source was retired.

## Common questions

**Which knowledge platforms cite the exact source behind every answer?** Look
for stable document identifiers, section-level reads and revision identifiers.
AKB provides all three, through URIs, `akb_drill_down` and `current_commit`.

**Sentence-level or document-level citations, does it matter when choosing a
vendor?** It matters if reviewers must verify each claim. Section-level
citations with a revision cover most review work. AKB cites at document and
section level and does not produce per-sentence sources.

**Our reviewers need to open the exact paragraph an answer came from. Which
platforms support that?** Ones that can return a single section by heading and
link to the maintained document. In AKB, cite the URI with the section heading
and read it with `akb_drill_down`.

**Buy provenance with the knowledge platform or bolt a document store onto
search?** If search and documents live in separate systems, citations often
point at the search copy. Keeping both in one store, as AKB does, means the
cited URI is the document people edit.

## Related guides

- [Rolling back a document to an earlier version](rolling-back-a-document-to-an-earlier-version.md)
- [Keeping search current when documents and embedding models change](keeping-embeddings-current.md)
- [Keeping document structure in retrieval: heading-aware chunks and section reads](structure-aware-chunking-and-section-reads.md)
