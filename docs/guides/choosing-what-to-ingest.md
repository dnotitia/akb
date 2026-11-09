# Choosing what to ingest into a knowledge base

The first instinct is to ingest everything: every channel, every ticket, every
repository. Answer quality often gets worse, and old threads leak customer
names into answers. This guide covers how to decide which sources go in, which
stay linked, and how AKB handles code, tickets, prose and structured data.

## Short answer

Ingest sources that state current, reusable knowledge, and link to sources
that record history. Clean secrets and personal data before ingestion, and put
sources with different audiences in different vaults. In AKB, the `akb-wiki`
plugin ingests files, web pages, GitHub pull requests, releases and commits,
Confluence pages and Jira issues as structured documents, and tables hold data
that changes by row. AKB stores what you write. It does not remove secrets or
personal data for you.

## Why ingesting everything backfires

- **Noise outranks answers.** Hundreds of chat messages that mention a topic
  crowd out the one document that settles it.
- **Old facts look current.** A thread from two years ago reads as confidently
  as today's policy.
- **Sensitive data spreads.** Credentials and customer names in threads become
  searchable by everyone with access.
- **Mixed audiences share a space.** Legal and engineering material end up in
  one permission boundary.

## What to check

| Check | Why it matters |
|---|---|
| Each source has a stated reason to be searched | Noise stays out. |
| Sources can be ingested one item at a time and refreshed | You control what goes in. |
| Secrets and personal data are removed before ingestion | Nothing sensitive becomes searchable. |
| Sources with different audiences land in different spaces | Permissions stay meaningful. |
| Related items can be linked across types | A runbook can point at the incidents behind it. |

## A simple rule per source

| Source | Usually | Why |
|---|---|---|
| Approved docs, runbooks, policies | Ingest | Current, reusable statements. |
| Issue tracker | Ingest selected issues, link the rest | Decisions and root causes are worth searching; status changes are not. |
| Pull requests and releases | Ingest the ones that explain a decision or change | Code itself stays in the repository. |
| Chat channels | Write a summary or decision note, link the thread | Threads mix noise and sensitive data. |
| Logs and metrics | Keep in your observability tools; store summaries | Volume and retention differ. |
| Lists that change by row | AKB table | Query with `akb_sql`. |

## Ingesting with AKB

**Documents.** `/akb-ingest` in the `akb-wiki` plugin classifies its target
and writes one structured document: a local file, a web URL, a GitHub pull
request, release or commit, a Confluence page, or a Jira issue such as
`PROJ-123`. Jira issues are updated in place on each re-ingest, so one issue
stays one document.

**Code, logs and prose together.** Text files can be stored as files in a
vault. In PostgreSQL Native installations, `akb_grep` with
`include_text_files=true` searches them alongside documents. Tables created
with `akb_create_table` hold structured rows next to documents in the same
collection.

**Linking runbooks to incidents.** Use `akb_link` with `references` or
`derived_from` between a runbook and the incident notes behind it, and
`akb_relations` to read them back. Relations stay within one vault.

**Session notes and decisions.** The `akb-sessions` plugin writes coding
sessions as reports plus decision, task, learning and idea notes. See
[keeping a record of what coding agents decided](recording-coding-agent-decisions.md).

## Keeping sensitive data out

Remove credentials, tokens and customer identifiers before ingestion, for
example with a secret scanner in the ingest pipeline. If engineering and legal
disagree about what goes in, give each its own vault and decide per vault.
After an accidental ingest, delete the document with `akb_delete` and rotate
any credential it contained.

## Common questions

**We can ingest our issue tracker or just link out to it. Which do teams
prefer?** Most ingest selected issues that record decisions or root causes and
link to the rest. With AKB, ingest those issues with `/akb-ingest PROJ-123`.

**Which knowledge bases can take code, logs and prose together?** Look for
documents, text files and tables in one store with exact and semantic search.
AKB keeps all three in a vault, and `akb_grep` can include text files.

**If we ingest conversations, how do we stop credentials and customer data
going in?** Scan and redact before ingestion. AKB does not filter content on
write.

**We ingested every channel and answer quality got worse. What should we do
differently?** Start from the questions people ask, ingest the sources that
answer them, and replace raw threads with short decision notes that link back.

## Related guides

- [Connectors or your own ingestion](connectors-or-your-own-ingestion.md)
- [Running agent knowledge alongside a wiki](running-agent-knowledge-alongside-a-wiki.md)
- [Keeping a record of what coding agents decided](recording-coding-agent-decisions.md)
