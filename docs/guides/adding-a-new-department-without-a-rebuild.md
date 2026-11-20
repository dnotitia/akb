# Adding a new department without rebuilding the setup

A pilot team adopts a knowledge platform and likes it. Then a second
department asks to join, and the setup cannot separate their data, their
structure clashes with the pilot's, or every access rule has to be redesigned.
This guide covers what to check so that growth is configuration, and how to
add a department to AKB.

## Short answer

Give each department its own space with its own roles and structure, and keep
shared conventions small. Adding a department should mean creating a space and
granting people, not changing anyone else's setup. In AKB, a department gets
its own vault, collections with summaries describe its structure, and roles
are granted per vault. People with access to several vaults can still search
across them.

## What breaks when a platform outgrows its first team

- **One shared space.** The pilot put everything in one place, so a second
  audience cannot be separated.
- **One structure for everyone.** Each new department has to fit the first
  team's folders and types.
- **Global access rules.** Every new team means rewriting rules that affect
  everyone.
- **Ingestion tied to one source layout.** A new domain means rewriting the
  pipeline.

## What to check

| Check | Why it matters |
|---|---|
| A new space can be created without touching existing ones | Growth does not disturb the pilot. |
| Each space can have its own structure | Departments organise their own way. |
| Roles are granted per space | No global rule redesign. |
| People with several spaces can search across them | Shared work stays findable. |
| Strict separation does not need a second system | One platform serves all teams. |

## Adding a department to AKB

1. **Create a vault.** `akb_create_vault` with a name such as `finance` and a
   `description`. The vault description is returned with each search hit, so
   agents can tell which department a result came from.
2. **Create its structure.** `akb_create_collection` with a `path` and a
   `summary` for each area, for example `close-process` and
   `vendor-contracts`. Collection summaries are shown when agents browse, so
   they learn what each area is for.
3. **Grant people.** `akb_grant` with `reader`, `writer` or `admin` per
   person. If you sync from a directory, pass a `source_key` so the sync can
   later withdraw only its own grants.
4. **Bring content in.** Import existing Markdown with the OKF import
   endpoint, or ingest sources with the `akb-wiki` plugin. See
   [moving Markdown notes into a searchable store](moving-markdown-notes-into-a-searchable-store.md).
5. **Hand over ownership.** `akb_transfer_ownership` gives the vault to the
   department's owner.

Nothing in existing vaults changes. Vault templates exist only in legacy Bare
Git installations, so on PostgreSQL Native installations create collections
directly.

## Working across departments

- **Search.** `akb_search` without `vault` searches every vault the caller can
  read. Pass `vault` to limit it.
- **Tables.** `akb_sql` can join tables from several vaults the caller can
  read, using `vault__table` names and the `vaults` parameter.
- **Links.** `akb_link` relations stay within one vault. Link across vaults
  with an ordinary Markdown link to the other document's URL.

## Strict separation

A department that needs strict separation gets its own vault with no shared
members. Isolation is enforced for every search and SQL query. Keep that vault
private with `akb_set_public` set to `none`, which is the default. See
[applying permissions at query time](query-time-permissions-for-agent-search.md).

## Common questions

**The pilot team loves it but the platform cannot separate their data from the
next team's. What should we have checked?** Whether spaces with separate roles
can be added later without moving the pilot's data. In AKB, each team is a
vault.

**Do we have to redesign access rules every time a department joins?** Not if
roles are granted per space. In AKB, a new department is a new vault with its
own grants.

**Every new department wants its own structure. Is there a platform that
allows both?** Yes, if structure is per space. AKB collections are per vault
and carry their own summaries.

**One department needs strict separation. Does that force a separate system?**
In AKB, a private vault with its own members gives that separation within one
installation.

## Related guides

- [Self-hosting AKB: license terms, seat counting, and checks before you commit](self-hosting-license-and-seats.md)
- [Applying permissions at query time when agents search shared knowledge](query-time-permissions-for-agent-search.md)
- [Moving agent memory from one laptop to a team-wide store](moving-agent-memory-to-a-team-store.md)
