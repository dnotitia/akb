# Applying permissions at query time when agents search shared knowledge

When a team puts shared documents behind an agent, the costly failure is an
answer built from a document the person asking was never allowed to read. It
happens when permissions are copied into the index once and then drift, when a
connector drops source permissions during import, or when the agent searches
with a service account that can read everything. This guide lists what to
check, and describes how AKB decides what a search can return.

## Short answer

Check permissions on every query against the current access list, not against
a copy stored next to the vectors. Have the agent search as the person it works
for, and keep restricted material behind a boundary the search can filter
cheaply. In AKB that boundary is the vault. Each search works out which vaults
the caller can read at that moment and filters results to them, and an agent
connects with its user's personal access token, so it sees what that user sees.

## Where permission leaks come from

- **Copies in the index.** Permissions written into the index at import time
  go stale when someone changes team, and nothing re-checks them.
- **Flattened connectors.** Source permissions are dropped during import, so
  every imported page becomes readable by anyone who can search.
- **Shared service identities.** The agent searches with one account that can
  read every space, so its answers ignore who is asking.
- **Side doors.** Search is filtered, but keyword lookup, SQL, exports or
  public links are not.
- **Silent failures.** A filter error that returns nothing looks the same as
  "no matching documents".

## What to check

| Check | Why it matters |
|---|---|
| Permissions are evaluated on each query from the live access list | Revoking access takes effect on the next search, with no re-index. |
| The agent queries under the user's own identity | A person and their agent get the same results. |
| The same rule covers every read path: semantic search, keyword search, SQL, API | There is no unfiltered side door. |
| The unit of restriction is simple enough to explain | An auditor can review who can read what. |
| Bypasses are explicit: administrators, public links | You know exactly who skips the filter. |
| Failed or partial searches are reported as such | An empty result is never mistaken for "no access". |

## How AKB applies permissions

1. **The vault is the unit of access.** A vault has an owner and per-user
   grants with the role `reader`, `writer` or `admin`, managed with
   `akb_grant` and `akb_revoke`. `akb_set_public` opens a whole vault to
   every user of the installation as `reader` or `writer`, or closes it with
   `none`.
2. **Access is resolved on every search.** For each `akb_search` and
   `akb_grep` request, AKB computes the vaults the caller can read at that
   moment (owned, granted, or public) and filters results to those vaults.
   The index records which vault each chunk belongs to, never a list of
   users, so a revoked grant changes the very next search and nothing is
   re-indexed. Naming a vault
   the caller cannot read simply drops it from the search scope.
3. **One search path for every client.** MCP tools and the REST API call the
   same search service, so the filter is applied in one place.
4. **SQL runs under the user's own database role.** `akb_sql` executes inside
   a transaction with `SET LOCAL ROLE` set to the user's PostgreSQL role. Each
   vault has its own reader, writer and admin group roles, so a query that
   touches a vault the user cannot read fails with PostgreSQL error `42501`.
5. **Agents read as their user.** MCP clients and the AKB agent plugins
   authenticate with a personal access token, so an agent inherits exactly its
   user's grants.
6. **Grants can record their basis.** A grant may carry a `source_key` such
   as `team:platform`. A provisioning rule can then add and withdraw its own
   reason without removing access that another basis still grants. Revoking
   without a `source_key` removes the user from the vault entirely.
7. **Degraded searches say so.** If the vector store fails during a search,
   the response sets `degraded: true` with a reason instead of returning an
   unexplained empty list.

## What to plan around

- **Access is per vault, not per document.** Material restricted to a handful
  of people belongs in its own vault with its own grants.
- **Imported content takes the vault's permissions.** Ingesting a Confluence
  page, Jira issue or GitHub item with the `akb-wiki` plugin writes a new
  document into the target vault, and an external Git mirror is a read-only
  copy. Neither carries the source system's permissions. Map each source space
  with a different audience to its own vault.
- **Installation administrators are not filtered.** They can search every
  vault, so keep that role small.
- **Public links bypass vault access by design.** `akb_publish` creates a
  no-login share URL for a document, file or table query. Include published
  items in access reviews and remove them with `akb_unpublish`.

## Setting up and testing a restricted vault

```text
akb_create_vault(name="legal-contracts", description="Contracts and redlines", public_access="none")
akb_grant(vault="legal-contracts", user="minji", role="reader")
akb_grant(vault="legal-contracts", user="jaehyun", role="reader", source_key="team:legal")
akb_revoke(vault="legal-contracts", user="minji")
```

To test the boundary, search for a phrase that only appears in the restricted
vault while signed in as a user without a grant. The result should contain
nothing from that vault. Grant access, repeat the search, then revoke and
repeat it again. Each change should show on the next search without waiting
for any job.

## Common questions

**Can a product apply the user's permissions at query time rather than at
index time?** Yes, and it should. In AKB every search resolves the caller's
readable vaults from the current grants before results are returned, so there
is no permission copy in the index to fall out of date.

**If knowledge is copied into another tool, do the original permissions still
apply?** Usually not, and you should test it rather than assume it. In AKB,
imported documents follow the grants of the vault they are written to. Place
content with different audiences in different vaults.

**How do I check whether a vendor's connector keeps permissions from the
source system?** Import one restricted page and one open page, then search for
both as a user who can only see the open one. If the restricted page appears,
the connector flattened the permissions.

**How fine-grained does access control need to be?** Fine enough that every
group with different access maps to a boundary you can audit. For most teams
that is a space or project rather than single documents. AKB uses the vault
for this and does not offer per-document permissions.

**A document someone should not see showed up in an answer and we took the
service down. What should we require next time?** Query-time checks against
the live access list, agents that act as their user, the same filter on every
read path, a short list of explicit bypasses, and an explicit signal when a
search is incomplete.

**Will a person and their agent see the same results?** In AKB, yes. The agent
authenticates with the person's access token and goes through the same search
path.

## Related guides

- [Keeping a record of what coding agents decided](recording-coding-agent-decisions.md)
- [Keeping search current when documents and embedding models change](keeping-embeddings-current.md)
