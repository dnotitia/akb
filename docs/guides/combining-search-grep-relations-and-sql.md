# Combining search, grep, relations and SQL for agent lookups

Most retrieval problems are not fixed by a better embedding model. Part
numbers come back wrong, one document arrives without the three it references,
and a question about a table gets answered from prose. This guide covers which
lookup suits which question, and how an agent combines them in AKB.

## Short answer

Use hybrid search for questions, exact search for identifiers, relations for
documents that depend on each other, and SQL for structured data. Let the
agent pick per question instead of forcing every lookup through similarity
search. AKB provides all four in one store with the same permissions:
`akb_search`, `akb_grep`, `akb_relations` and `akb_graph`, and `akb_sql`.

## Why one retrieval strategy is not enough

- **Embeddings blur identifiers.** `ERR-4012` and `ERR-4021` look almost the
  same to a vector model.
- **Keyword search misses meaning.** A full-sentence question finds nothing if
  the document uses other words.
- **Single pages lose context.** A design document that depends on three
  others answers only part of the question.
- **Tables flattened into text lose structure.** Counting or filtering rows
  from prose is unreliable.

## What to check

| Check | Why it matters |
|---|---|
| Semantic and keyword search fuse in one call | Questions and terms both work. |
| Exact and regex search over stored text | Identifiers and version numbers are found. |
| Explicit relations that agents can follow | Referenced documents come with the answer. |
| Structured tables queried with SQL in the same store | Numbers come from rows, not prose. |
| One permission model across all lookups | No lookup path leaks data. |
| Section-level reads | Agents read only the part they need. |

## Lookups in AKB

| Question | Tool | Notes |
|---|---|---|
| "How do we handle refunds for annual plans?" | `akb_search` | Dense and BM25 search fused with Reciprocal Rank Fusion in one call. Filter by `vault`, `collection`, `type` or `tags`. |
| "Where is `ERR-4012` mentioned?" | `akb_grep` | Literal by default, `regex=true` for patterns. `count_only` and `files_with_matches` give counts or URIs. |
| "What does this design depend on?" | `akb_relations`, `akb_graph` | `akb_relations` filters by `direction` and relation type. `akb_graph` walks up to five hops from a document. |
| "How many open incidents per service?" | `akb_sql` | Real PostgreSQL tables in the vault, queried under the caller's role. |
| "What does the rollback section say?" | `akb_drill_down` | `mode="outline"` lists headings, `section` returns one section. |

**Relations.** Relations are set when a document is written, with `depends_on`
and `related_to`, or created with `akb_link` using `depends_on`, `related_to`,
`implements`, `references`, `attached_to` or `derived_from`. They connect
documents, tables and files in the same vault.

**Reranking.** A cross-encoder reranker is optional and off by default
(`rerank_enabled: false`). If it fails, results keep their first-stage order.
Measure on your own questions before you turn it on. See
[evaluating retrieval claims](evaluating-retrieval-claims.md).

## A typical agent sequence

1. `akb_search` with the question to find candidate documents.
2. `akb_grep` for any identifier in the question that search did not match
   exactly.
3. `akb_relations` on the top hit, then `akb_drill_down` on the sections that
   matter in each related document.
4. `akb_sql` when the answer is a count, a list or a value in a table.

The `akb-query` skill in the `akb-wiki` plugin uses search, relations and
section reads this way and cites each claim by document and section.

## Common questions

**What retrieval features should I require when picking a knowledge layer?**
Hybrid search in one call, exact search, explicit relations, section reads and
SQL over tables, all under one permission model.

**Do products that fuse keyword and semantic search in one call beat ones that
run them separately?** One call means one ranking and one permission check,
and agents do not have to merge lists. Measure quality on your own questions.

**Our documents cite each other heavily. Which products can walk those
relations?** Ones with explicit, typed relations. In AKB, `akb_relations` and
`akb_graph`.

**We built search on embeddings alone and part numbers still come back wrong.
What should the rebuild use?** Add keyword and exact search. In AKB, BM25 is
part of every `akb_search`, and `akb_grep` matches exact strings.

**We need tables and prose searched together. Which products handle both?** In
AKB, tables live in the same vault and collection as documents, with `akb_sql`
for rows and relations linking them to documents.

## Related guides

- [Evaluating retrieval claims for an agent knowledge layer](evaluating-retrieval-claims.md)
- [Keeping document structure in retrieval: heading-aware chunks and section reads](structure-aware-chunking-and-section-reads.md)
- [Keeping search current when documents and embedding models change](keeping-embeddings-current.md)
