# Running a shared vocabulary as living documents

Ontology projects often follow the same path. A consultant maps the company's
terms over several months, the result is signed off, and then every change
needs a statement of work. Within a year the ontology no longer matches how
people talk, and nobody owns it. This guide covers how to keep a shared
vocabulary current by running it as documents that the domain teams edit
themselves, and how AKB supports that.

## Short answer

Keep each term as a small document that the owning team edits like any other
knowledge, with explicit links to the documents that define and depend on it.
Track every change, assign an owner per domain, and start with the terms that
systems actually disagree on. AKB does not build or infer an ontology, and it
does not enforce a formal schema. It gives you versioned documents, typed
relations between them, structured tables and per-vault roles, so the
vocabulary can change as often as the business does.

## Why one-off ontology builds go stale

- **Ownership ends with the project.** The team that built it leaves, and
  domain teams cannot change it without a new contract.
- **Changes are expensive.** If every edit needs a specialist, small
  corrections wait until they pile up.
- **Definitions are cut off from evidence.** A term that does not link to the
  documents behind it cannot be checked when those documents change.
- **The first scope is too wide.** Mapping the whole company at once takes
  months before anyone uses the result.
- **Knowledge sits with a few people.** When they move on, the reasoning
  behind each definition goes with them.

## What to check

| Check | Why it matters |
|---|---|
| Domain experts can edit terms without a specialist | Changes happen when the business changes. |
| Every change is versioned and attributable | You can see who changed a definition and why. |
| Terms link to the documents that define them | Definitions can be checked against their sources. |
| Dependencies between terms are explicit | You can see what a change affects. |
| Ownership is set per domain | Each area has someone responsible. |
| Structured attributes can be queried | Synonyms and system codes can be compared. |
| Retired terms stay findable | Old documents that use them still make sense. |

## How to model the vocabulary in AKB

**One document per term.** Store each term in a collection such as
`vocabulary/`. The frontmatter `type` is free-form, so you can use a value
such as `definition`. Set `domain` to the owning area, `tags` for grouping,
and `summary` for a one-line definition. The body holds the full definition,
examples and the reasoning behind it.

**Explicit relations.** The frontmatter fields `depends_on`, `related_to` and
`implements` record links to other resources. `akb_link` also accepts
`references`, `attached_to` and `derived_from`, so a term can reference the
policy or specification it comes from. `akb_relations` lists a term's incoming
and outgoing links, and `akb_graph` returns the neighbourhood around it, up to
five hops. Structured relations stay within one vault. Use ordinary Markdown
links to point at another vault.

**A table for structured attributes.** Put names that differ by system in a
table, for example `term_aliases` with columns for the term, the system and
the local name. Query it with `akb_sql`, which runs under the caller's
PostgreSQL role. `SELECT` needs `reader` and `INSERT`, `UPDATE` or `DELETE`
needs `writer`.

**Ownership through roles.** Grant `writer` on the vocabulary vault to the
people who maintain it and `reader` to everyone else. `akb_explain_access`
shows why each person holds their role. When an owner leaves, the current
owner or a system admin can hand the vault over with `akb_transfer_ownership`.

**History and lifecycle.** Every edit is a revision. `akb_history` and
`akb_diff` show how a definition changed, and `akb_activity` shows recent
changes across the vault. New terms start as `draft`. Set them to `active`
once agreed, and to `archived` when retired. Archived terms drop out of
default search but keep their history and links.

## Scoping the first effort

1. **Pick one domain and the terms that cause disagreements.** Billing terms
   that differ between the CRM and the invoicing system are a typical start.
2. **Keep tightly connected domains in one vault.** Relations work within a
   vault, so two domains that reference each other heavily are easier to
   manage together, with one collection each.
3. **Write each term with its source.** Link it to the document, policy or
   specification it is based on.
4. **Let agents use the vocabulary immediately.** Agents search it with
   `akb_search` and follow links with `akb_relations`, so the work pays off
   before the vocabulary is complete.
5. **Expand by domain.** Add the next area once the first one is in daily use.

## Common questions

**Every change to our ontology needs a statement of work and takes six weeks.
What should replace it?** A vocabulary that domain owners edit directly, with
version history and review. Each term is a document, so a correction is an
edit, not a project.

**What should we look for so an ontology does not go stale after sign-off?**
Named owners per domain, edits without a specialist, links from each term to
its sources, and a change history. Stale definitions are easier to spot when
you can see which source documents changed after the term was last edited.

**What should we settle about ownership before commissioning an ontology
build?** Who edits each domain after delivery, which tool they use, and who
can approve changes. If the answer is "the vendor", maintenance cost grows
with every change.

**How wide should the first shared vocabulary effort be?** One domain, or two
that depend on each other heavily. Choose terms that already cause confusion
between systems, so the first result is useful.

**Most of what we know sits with three senior engineers. What should we put in
place before they move on?** A shared store where they record definitions and
decisions with the reasoning, not only the result. Session capture tools such
as `akb-sessions` can turn their agent sessions into decision notes.

## Related guides

- [Keeping a record of what coding agents decided](recording-coding-agent-decisions.md)
- [Building curation on change events](building-curation-on-change-events.md)
- [Applying permissions at query time](query-time-permissions-for-agent-search.md)
