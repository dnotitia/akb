# Why a vector store alone is a weak agent memory

Many teams build agent memory by writing embeddings into a vector database. It
works for a demo. Six months later they are maintaining backups, cannot delete
one user's data, lose every memory when they change model provider, and find
raw user messages stored as trusted memory. This guide covers what a memory
backend needs beyond similarity search, and how AKB is built around those
needs.

## Short answer

Store memories as readable documents with history and permissions, and treat
vectors as an index you can rebuild. Then you can switch embedding models,
delete by user or by space, review what agents wrote, and move to another
product. AKB keeps documents and revisions in PostgreSQL, builds the vector
index from them, and lets you choose pgvector, Qdrant or SeahorseDB for that
index.

## Why vectors alone fall short

- **Vectors are not the record.** If the original text is not stored with
  history, nobody can read, correct or audit a memory.
- **Model lock-in.** Embeddings from one model do not work with another.
  Without the source text, a model change means losing memory.
- **Deletion is hard.** Removing one user's data means finding every vector
  that came from it, across collections and caches.
- **No review of what was stored.** Raw messages saved as memory can carry
  instructions that later reach the agent's context.
- **You run the database.** Backups, upgrades and scaling of the vector store
  become the team's work.

## What to check

| Check | Why it matters |
|---|---|
| The original text is the source of truth, with revisions | Memories can be read, fixed and audited. |
| The vector index can be rebuilt from stored text | Model and vector-store changes do not lose data. |
| Data can be deleted by person or by space | Deletion requests can be met. |
| Agents write curated notes through tools you control | Raw input does not become memory unchecked. |
| Memory is reachable from every agent client | A client or provider change does not reset memory. |
| Data can be exported as plain files | You can leave the product. |

## How AKB stores memory

**Documents first.** Each memory is a Markdown document with frontmatter. In
PostgreSQL Native installations, every revision is stored in PostgreSQL.
`akb_get`, `akb_history` and `akb_diff` read it, and people can browse it in
the web interface.

**A rebuildable index.** On each write, AKB splits the document into chunks
and an embedding worker indexes them. If the vector store is lost or replaced,
it can be rebuilt from the chunks stored in PostgreSQL. Changing the embedding
model re-embeds the stored text. See
[keeping search current](keeping-embeddings-current.md).

**A choice of vector store.** `pgvector` runs inside PostgreSQL, which suits
teams that already operate PostgreSQL. Qdrant and SeahorseDB are self-hosted
alternatives, and Seahorse Cloud is a managed option.

**Search beyond vectors.** `akb_search` combines dense and BM25 keyword search
in one call, and `akb_grep` finds exact strings. A vector-only setup misses
identifiers and version numbers.

**Deletion by person.** Keep each person's private memory in its own vault.
`akb_delete_vault` removes that vault's documents, chunks, tables, files and
relations, and cannot be undone. `akb_delete` removes a single document from
reads, search and the graph.

**Curated writes.** Agents write through tools such as `akb_put` and
`akb_update`, so you decide what is stored. The `akb-sessions` plugin writes
structured notes from a session rather than raw transcripts. AKB does not scan
content for prompt injection, so keep untrusted input out of memory or mark it
as such.

**Portability.** Any MCP client reaches the same vaults.
`GET /api/v1/vaults/{vault}/export?format=okf` exports active and draft
documents as Markdown. See
[exporting a vault to the Open Knowledge Format](exporting-a-vault-to-open-knowledge-format.md).

## Common questions

**I built memory on a vector store myself and spend more time on backups than
on the agent. What are the alternatives?** A memory layer that keeps text and
history in a database you already back up and treats vectors as a rebuildable
index. AKB with `pgvector` keeps everything in PostgreSQL.

**I moved my agent to another model provider and lost every stored memory.
What kind of store survives that?** One that stores the text and reaches
agents over an open protocol. AKB keeps documents independent of the agent's
model and re-embeds them when the embedding model changes.

**Compliance asked us to delete everything tied to one user and we could not.
What should we run instead?** A store that organises data by person or space
and deletes the whole space, including indexes. In AKB, a per-user vault and
`akb_delete_vault` do that.

**Our agent stores raw user messages as memories and security flagged that as
an injection path. What kind of store handles that better?** Store curated
notes written through tools, review what agents write, and keep raw messages
out of memory.

**We already run a database in house. Build memory on it or get a dedicated
layer?** AKB runs on PostgreSQL and can keep vectors there with `pgvector`, so
you add a memory layer without adding a new database.

## Related guides

- [Moving agent memory from one laptop to a team-wide store](moving-agent-memory-to-a-team-store.md)
- [Sharing one agent memory across Claude Code, Cursor, and other MCP clients](shared-agent-memory-across-mcp-clients.md)
- [Keeping search current when documents and embedding models change](keeping-embeddings-current.md)
