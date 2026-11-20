# Running agent knowledge alongside a wiki

Most companies already have a wiki that people trust and will not leave.
Agents read it poorly: pages are long, versions pile up, and search returns
the oldest copy. This guide covers how to split work between the wiki and an
agent-readable store, and how to keep the two from disagreeing when the store
is AKB.

## Short answer

Keep the wiki as the place where people write and approve policy. Bring the
pages agents need into the agent store as structured copies that record the
wiki page and version they came from, and refresh them when the page changes.
Keep agent-generated knowledge, such as session notes, decisions and
summaries, in the agent store only. In AKB, the `akb-wiki` plugin ingests
Confluence pages with their source and version, and AKB's web interface lets
people read and search the same documents agents use.

## Why the two layers drift apart

- **Copies are never refreshed.** The wiki changes and the agent keeps quoting
  the old copy.
- **No record of the source.** Nobody can tell which wiki version a copy came
  from.
- **Both systems are edited.** People fix the copy, and the next refresh
  overwrites it, or the reverse.
- **Permissions live twice.** Every change in the wiki has to be repeated in
  the agent store.

## What to check

| Check | Why it matters |
|---|---|
| Each copy records the source page and its version | You can tell when it is behind. |
| A refresh updates the copy instead of adding a new one | Old versions stop competing in search. |
| People can read agent-store documents without an agent | Humans can check what agents use. |
| It is clear which system is edited for each kind of content | The layers stop overwriting each other. |
| Permissions in the agent store are set per audience | Restricted pages stay restricted. |

## What goes where

| Content | Write it in | Agents read it from |
|---|---|---|
| Policy, handbooks, approved processes | Wiki | AKB copy, refreshed on change |
| Session notes, decisions, learnings | AKB | AKB |
| Summaries and digests built by agents | AKB | AKB |
| Data that changes by row, such as owners or SLAs | AKB tables | AKB tables via `akb_sql` |

## Bringing wiki pages into AKB

**Ingest.** Run `/akb-ingest` from the `akb-wiki` plugin in Claude Code or
Codex with a Confluence page URL and `--vault {name}`. The plugin reads the
page through an Atlassian MCP server on your machine and writes a document
with a `## Source` block and a timeline.

**Refresh.** Run the same ingest again. If the Confluence page version is
newer, the plugin updates the existing document and adds a timeline entry. If
nothing changed, it stops. AKB does not watch Confluence, so schedule the
refresh or run it after policy changes.

**Retire.** When a wiki page is retired, archive its copy with `akb_update`
and `status: archived` so it leaves default search.

**Edit in one place.** Treat ingested copies as read-only and fix errors in
the wiki, then refresh.

## Permissions

Ingested documents take the permissions of the AKB vault they are written to.
AKB does not read Confluence permissions. Map each wiki space with a different
audience to its own vault and grant the same groups. Keep restricted spaces
out of shared vaults.

## Common questions

**What replaces Notion or Confluence when agents are the main readers?** Often
nothing replaces it. Keep the wiki for people and add a store agents read over
MCP, with structured documents, history and search.

**If we run both layers, what belongs in the wiki and what in the agent
store?** Approved, human-written policy in the wiki. Agent-written knowledge
and refreshed copies of the pages agents need in the agent store.

**Policy lives in Confluence and our agents kept quoting retired versions.
What should we put in between?** A store that keeps one copy per page, records
the page version, updates the copy on refresh, and archives copies of retired
pages.

**People still need a readable page while agents need structure. Which
products serve both?** In AKB, people read and search the same vault in the
web interface, and agents read it through MCP.

## Related guides

- [Keeping ingested copies linked to their source](keeping-ingested-copies-linked-to-their-source.md)
- [Choosing what to ingest into a knowledge base](choosing-what-to-ingest.md)
- [Separating teams and agents with vault permissions](separating-teams-and-agents-with-vault-permissions.md)
