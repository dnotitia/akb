# Reconciling conflicting names across systems

The same customer is an account in the CRM, a tenant in billing and an org in
support. Two acquired companies name their products in opposite ways, and both
insist theirs is right. This guide covers how to record which names mean the
same thing, keep the evidence for each mapping, and rename consistently, using
AKB.

## Short answer

Keep the mapping as data, with one row per name per system, and link every
mapping to the document that justifies it. Choose a preferred name per
concept, but keep the others searchable. Rename in bulk only after a review.
In AKB, a table holds the mappings, relations link each concept to its
evidence, and `akb_grep` finds and replaces old names across documents. AKB
does not propose or infer mappings. People or your own agents decide them.

## Why name cleanups stall

- **Mappings without evidence.** Nobody remembers why two names were declared
  equal, so every mapping is reopened.
- **A one-time cleanup.** Names are fixed once, and new systems bring new
  conflicts.
- **Renames that miss copies.** Old names survive in documents and keep
  confusing search.
- **One side wins by decree.** The other team keeps using its own name anyway.

## What to check

| Check | Why it matters |
|---|---|
| Each mapping shows the evidence behind it | Disputes are settled once. |
| Every system's name stays searchable | People find things under the name they know. |
| A rename can be previewed before it is applied | Mistakes are caught early. |
| Mappings and renames have history | You can see who changed what. |
| New conflicts can be added without a project | The mapping stays current. |

## Recording names in AKB

**A mapping table.** Create a table with `akb_create_table`, for example
`name_map` with columns `concept`, `system`, `local_name`, `preferred`
(boolean) and `evidence_uri` (text). Query it with `akb_sql`, which needs
`reader` for `SELECT` and `writer` for changes.

```sql
SELECT system, local_name FROM name_map WHERE concept = 'customer' ORDER BY system
```

**A document per concept.** Each concept has a short document that states the
preferred name, lists the other names, and explains the decision. List every
name in the body so `akb_search` and `akb_grep` find the document under any of
them.

**Evidence links.** Link the concept document to the sources that justify it
with `akb_link`, using `derived_from` or `references`. Link it to the mapping
table too. `akb_relations` shows the evidence, and `akb_provenance` shows who
created the document and when. Relations stay within one vault, so keep
concept documents and evidence together.

**Renaming documents.** `akb_grep` with `count_only=true` shows how many
documents use an old name. With `replace`, it rewrites the matches across the
scope. If the scope exceeds `max_replacements`, nothing is written, so set it
to the count you reviewed. Each change is recorded in history, and
`akb_activity` lists the changed documents.

**Ownership.** Give each domain an owner with `writer` on the vault, and
review changes with `akb_diff`.

## Common questions

**What do teams use when the same thing has different names in every system?**
A mapping table with evidence per row, a document per concept, and search that
finds it under any name.

**Two acquired subsidiaries use opposite naming conventions. What tooling
helps?** One that records both names, the reason for the preferred one, and
the evidence. Keep both names searchable so neither team loses its vocabulary.

**How can we trace a definition back to the document it came from?** Explicit
links from the concept to its sources. In AKB, `derived_from` and `references`
relations, read with `akb_relations`.

**How do we verify that a product shows the evidence behind each mapping it
proposes?** Ask it to show the source for five mappings you know. With AKB,
the evidence is in the `evidence_uri` column and in the concept document's
relations.

## Related guides

- [Running a shared vocabulary as living documents](running-a-shared-vocabulary-as-living-documents.md)
- [Citing the exact source behind an answer](citing-the-exact-source-behind-an-answer.md)
- [Finding duplicate and outdated documents](finding-duplicate-and-outdated-documents.md)
