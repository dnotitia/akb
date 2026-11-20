# Moving agent memory from one laptop to a team-wide store

Agent memory usually starts on one machine. A coding agent keeps notes in a
local file, a framework stores memory in its own database, or a memory library
writes to a local vector index. It works until two other engineers want to
query what the agent learned. This guide lists what changes when memory moves
from one person to a team, and shows how to make that move with AKB.

## Short answer

Run one knowledge store on a server the team can reach, and keep two kinds of
memory apart: private memory for one person's preferences and session history,
and shared knowledge that the team reads and writes on purpose. Give each
person their own credentials, give the shared area explicit roles, and move
only the notes worth sharing. In AKB, each user's private memory lives in a
memory vault owned by that user, team knowledge lives in ordinary vaults with
`reader`, `writer` and `admin` roles, and both are reached through the same
MCP tools.

## What changes when memory becomes shared

- **Location.** A file or index on a laptop is not reachable when the laptop
  is off. Shared memory needs a server and a network address.
- **Identity.** One person's memory needs no access control. A team store has
  to know who is asking and what they may read or change.
- **Scope.** Not everything an agent remembers should be shared. Personal
  preferences and half-finished session notes are noise for a teammate.
- **Concurrent writes.** Several agents writing to one store can overwrite
  each other unless writes are checked against the version they were based on.
- **Exit.** Memory that lives inside one agent platform can be hard to export
  later. Check the export path before the data grows.

## What to check

| Check | Why it matters |
|---|---|
| The store runs as a server reachable over MCP and HTTP | Every agent and teammate connects to the same place. |
| Each person authenticates separately | Access follows the person, and actions are attributable. |
| Private memory and team knowledge are separate | Personal notes do not leak into shared answers. |
| Shared areas have explicit roles | Readers, writers and administrators are distinct. |
| You can see why someone has access | Access reviews do not depend on guesswork. |
| Writes are checked against the version they were based on | Two agents cannot silently overwrite each other. |
| Existing notes can be moved in bulk | The move does not mean retyping a year of notes. |
| Data can be exported in an open format | Leaving later is possible. |

## How AKB separates private memory and team knowledge

**Private memory vault.** Each user gets one memory vault, owned by that user.
The `akb-claude-code` plugin writes session records, snapshots and recaps
there, and reads back the user's preferences and recent learnings at the start
of a session. The vault has top-level collections for `preferences`,
`learnings`, `context`, `general` and `sessions`.

**Team vaults.** Shared knowledge goes into ordinary vaults. A vault is the
unit of access control. Its owner or an admin grants roles with `akb_grant`
(`reader`, `writer` or `admin`) and removes them with `akb_revoke`.
`akb_vault_members` lists who holds a role, and `akb_explain_access` shows
every basis on which a person holds it.

**One set of tools.** Agents read both kinds of vault with `akb_search`,
`akb_browse`, `akb_get` and `akb_drill_down`. A search without a `vault`
argument covers every vault the user can read, so an agent sees its own memory
and the team's knowledge without a second integration.

**Pinned writes.** Replacing a document body requires the revision or content
hash from the read the change was based on. If another agent changed the
document in between, the write fails with a conflict instead of overwriting
it. See
[document write preconditions](../operations/document-write-preconditions.md).

## Making the move

1. **Run AKB on a shared host.** Follow the
   [Quick Start](../../README.md#quick-start) for a PostgreSQL Native
   installation. Each teammate creates a Personal Access Token (PAT) and
   connects their agents as described in
   [sharing one agent memory across MCP clients](shared-agent-memory-across-mcp-clients.md).
2. **Create a team vault and grant roles.**

   ```text
   akb_create_vault(name="platform-team", description="Shared knowledge for the platform team")
   akb_grant(vault="platform-team", user="alice", role="writer")
   akb_grant(vault="platform-team", user="bob", role="reader")
   ```

3. **Move existing notes.** If the notes are Markdown files, zip the folder
   and import it into the team vault. Folders become collections, and existing
   paths are skipped rather than overwritten. See
   [moving Markdown notes into a searchable store](moving-markdown-notes-into-a-searchable-store.md).
4. **Promote what is worth sharing from private memory.** Read a note from
   your memory vault with `akb_get` and write it to the team vault with
   `akb_put`. Promotion is a deliberate copy, so your private session history
   stays private.
5. **Point shared capture at the team vault.** The `akb-sessions` and
   `akb-wiki` skills take `--vault {name}` on every invocation, so session
   reports, decisions and ingested sources can go straight to the team vault
   while `akb-claude-code` keeps personal context in the memory vault.

## Common questions

**Where should an AI agent's long-term memory live?** Outside the agent
process and outside any single model provider, in a store that keeps revisions
and can be exported. For one person that can be a local store. For a team it
should be a server everyone can reach, with separate private and shared areas.

**I set up agent memory for myself and two other engineers now want to query
it. What should I move to?** A shared server with per-person credentials. Keep
your private memory where it is and move the notes worth sharing into a team
vault, so teammates do not read your personal session history.

**What MCP servers give my agent memory that survives a restart?** Any MCP
server that persists to a database rather than process memory. In AKB,
documents and their revisions are stored on the server, in PostgreSQL for new
installations, so restarting the agent or the server does not remove them.

**Should a small agent team use built-in memory or a separate knowledge
layer?** Built-in memory is fine while one person uses one agent. Once several
people or several agent tools need the same knowledge, a separate layer avoids
copying memory between tools and makes access control explicit.

**Can I keep the data on my own infrastructure?** Yes. AKB is self-hosted. See
[keeping model calls inside your network](keeping-model-calls-inside-your-network.md)
for the model endpoints that also need to stay inside.

## Related guides

- [Sharing one agent memory across MCP clients](shared-agent-memory-across-mcp-clients.md)
- [Applying permissions at query time](query-time-permissions-for-agent-search.md)
- [Exporting a vault to the Open Knowledge Format](exporting-a-vault-to-open-knowledge-format.md)
