# Keeping document structure in retrieval: heading-aware chunks and section reads

Fixed-length chunking can separate a rule from its exception. If chunks also
lack document metadata and heading paths, an agent may retrieve a passage
without enough context to interpret it correctly. This guide explains how AKB
uses Markdown headings, contextual metadata, and section reads to help agents
retrieve relevant passages and inspect their surrounding context.

## Short answer

Split on the document's own headings, carry the heading path and document-level
metadata into every chunk, split oversized sections at paragraph boundaries
with overlap, and give the agent a way to read the outline first and open only
the section it needs. AKB does all of this for Markdown documents: chunks
follow headings, each chunk starts with its title and heading path, and
`akb_drill_down` returns either a document's outline or the body of selected
sections.

## Why structure gets lost

- **Boundaries ignore meaning.** A fixed window does not know where a section
  ends, so related sentences are separated.
- **Chunks lose their address.** Without the heading path, `must not be used
  for production data` could belong to any section of any document.
- **Short chunks win by accident.** A short chunk from an unrelated document
  that repeats the query terms can outrank the correct chunk buried in a long
  one.
- **Agents read too much or too little.** Loading whole documents wastes
  context; loading a single chunk drops the surrounding rules.

## What to check

| Check | Why it matters |
|---|---|
| Splits on headings rather than a fixed window | Sections stay intact. |
| Keeps the heading hierarchy with each chunk | The chunk keeps its place in the document. |
| Adds document title and summary to each chunk | Deep chunks still match document-level queries. |
| Prefers paragraph boundaries when splitting oversized sections, with overlap | Helps preserve nearby context. A paragraph that exceeds the size limit may still require a character-based split. |
| Handles text without headings honestly | Plain files are split by size without inventing structure. |
| Offers an outline read and a section read | The agent can skim, then open only what it needs. |
| Can still return the whole document | Some questions need everything. |

## How AKB splits Markdown documents

When a document is written, AKB rebuilds its chunks:

1. **Heading split.** The body is split at Markdown headings (`#` to
   `######`). Each chunk records its heading hierarchy as a section path, such
   as `# Deployment > ## Backups > ### Retention`.
2. **Context prefix.** Each chunk's text begins with that section path in
   brackets, so both keyword and semantic search see where it sits.
3. **Document header.** Every chunk of the document also starts with a short
   header: `TITLE`, `SUMMARY` and `TAGS` from the frontmatter when present,
   the vault path, and the document `TYPE`. This keeps a chunk from deep in
   the body tied to its document.
4. **Size bound.** A section longer than 1,500 characters is split at blank
   lines between paragraphs. When a split happens, the last 200 characters of
   the previous chunk are carried into the next one. A single paragraph longer
   than the limit is split by characters as a last resort.
5. **Text files.** Plain text files are split by size only and get no section
   path, because there is no structure to claim.

These additions exist for indexing. Search results drop the document header,
and section reads also drop the heading line and the repeated overlap, so
agents read the original text once.

## How agents read by section

`akb_drill_down` has two modes:

- `mode="outline"` returns only the heading paths of a document, up to 50,
  so an agent can see its structure cheaply.
- `mode="sections"` (the default) returns the body of sections whose heading
  matches `section`, optionally narrowed by a `pattern` found in the body.

```text
akb_drill_down(uri="akb://eng/coll/runbooks/doc/backups.md", mode="outline")
akb_drill_down(uri="akb://eng/coll/runbooks/doc/backups.md", section="Retention")
```

If nothing matches, the response includes the outline and suggests retrying
with a listed heading or reading the whole document with `akb_get`.

A typical flow is `akb_search` to find candidate documents, `akb_drill_down`
with `mode="outline"` to see their structure, then `akb_drill_down` on the
one section that answers the question.

## Writing documents that retrieve well

The chunker can only keep structure that the document has:

- Put an exception under the same heading as the rule it modifies.
- Use descriptive headings. `Retention for audit logs` retrieves better than
  `Notes`.
- Fill in `summary` in the frontmatter. It is added to every chunk.
- Link related documents with `depends_on`, `related_to`, or `implements` so
  agents can follow them with `akb_relations`.
- Keep tabular data in AKB tables (`akb_create_table`, `akb_sql`) when an
  agent needs to filter or aggregate it.

## Common questions

**Which products keep long documents from losing context when they are split?**
Look for heading-based splitting, a heading path and document metadata on every
chunk, and paragraph-boundary splits with overlap. AKB applies all of these to
Markdown documents.

**Can an agent inspect the headings first and open only the section it needs?**
Yes. In AKB, `akb_drill_down` with `mode="outline"` lists heading paths rather
than generated summaries. A second call with `section` returns the matching
section's text.

**What if a clause still ends up in a different chunk from its rule?** Keep the
two under one heading and below the size limit, or read the section with
`akb_drill_down` rather than relying on a single retrieved chunk.

## Related guides

- [Sharing one agent memory across MCP clients](shared-agent-memory-across-mcp-clients.md)
- [Evaluating retrieval claims for an agent knowledge layer](evaluating-retrieval-claims.md)
- [Self-hosting AKB: license terms and seat counting](self-hosting-license-and-seats.md)
