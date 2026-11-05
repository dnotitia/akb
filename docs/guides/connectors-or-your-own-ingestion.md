# Connectors or your own ingestion

Knowledge platforms are often compared by the length of their connector list.
Then the one connector a team needs is in beta, permissions are flattened on
the way in, or a vendor upgrade breaks every custom connector in a week. This
guide covers how to evaluate ingestion, and what ingestion paths AKB offers.

## Short answer

Judge ingestion by the interfaces you will build on, not by the connector
count. Look for a documented write API, an open import format, and agent-side
ingestion you can read and change. AKB has no server-side connector catalogue.
It ingests through the `akb-wiki` plugin, the OKF import endpoint, the MCP
write tools and the REST API, and every path writes plain Markdown documents
you can inspect.

## Why connector lists mislead

- **Coverage is uneven.** A listed connector may skip attachments, comments or
  history.
- **Permissions get flattened.** Many connectors copy content without the
  source system's access rules.
- **Custom connectors break on upgrade.** Private or unstable APIs change
  under you.
- **One person owns the pipeline.** Nobody else can fix it when it stops.

## What to check

| Check | Why it matters |
|---|---|
| The write API and import format are documented and versioned | Your own ingestion survives upgrades. |
| Connector code is readable and changeable | You can fix the one you need. |
| You know how source permissions map to the store | Restricted content stays restricted. |
| Failed items are reported, not skipped silently | You know when ingestion breaks. |
| Re-running ingestion updates or skips instead of duplicating | Retries are safe. |

## Ingestion paths in AKB

**akb-wiki plugin.** `/akb-ingest` runs in Claude Code or Codex and writes one
document per target: a local file, a web URL, a GitHub pull request, release
or commit, a Confluence page or a Jira issue. Fetching happens on the agent's
machine, through `git`, `gh`, web fetch or an Atlassian MCP server. The ingest
logic is Markdown agent instructions in the repository's `plugins/` directory,
so you can read and adapt it.

**OKF import.** `POST /api/v1/vaults/{vault}/import?format=okf` takes a zip of
Markdown files and needs `writer`. Existing paths are skipped, failures are
listed per file, and the response reports created, skipped and failed counts.
Agents can call `akb_import` with the same bundle. See
[moving Markdown notes into a searchable store](moving-markdown-notes-into-a-searchable-store.md).

**MCP and REST writes.** Your own pipeline can call `akb_put` and `akb_update`
over MCP, or the REST API with the reference client in `packages/akb-client`.
Whole-body updates must pass the revision they were based on, so a pipeline
cannot silently overwrite a newer edit.

**Change events out.** If downstream systems need to react to ingestion, read
AKB's versioned change events. See
[resource change events](../operations/change-events.md).

**Git mirroring** of an external repository exists only in legacy Bare Git
installations. PostgreSQL Native installations reject it.

## Permissions

None of these paths copies permissions from the source system. A document
takes the permissions of the vault it is written to. Map each source audience
to a vault and grant the same people. See
[separating teams and agents with vault permissions](separating-teams-and-agents-with-vault-permissions.md).

## Common questions

**How do I find out whether a vendor's connector keeps permissions from the
source system?** Ask for the mapping, then test with a restricted item and an
account that should not see it. In AKB the answer is fixed: permissions come
from the target vault.

**We wrote our own connectors and a vendor upgrade broke them. What should we
move to?** A platform whose write API and import format are documented and
versioned. AKB's MCP tools, OKF import and `ChangeEventEnvelopeV1` events are
public interfaces in the repository.

**Built-in connectors or our own ingestion?** Use existing ingestion for
common sources and your own for the rest, both on public interfaces. With AKB,
start from `akb-wiki` and write the rest against `akb_put` or OKF import.

**Our ingestion works but only one engineer can maintain it. What removes that
risk?** Ingestion written as readable agent instructions or small scripts on a
documented API, with per-item error reports.

## Related guides

- [Choosing what to ingest into a knowledge base](choosing-what-to-ingest.md)
- [Keeping ingested copies linked to their source](keeping-ingested-copies-linked-to-their-source.md)
- [Moving Markdown notes into a searchable store](moving-markdown-notes-into-a-searchable-store.md)
