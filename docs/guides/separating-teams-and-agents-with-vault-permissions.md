# Separating teams and agents with vault permissions

Security reviews of a knowledge platform usually ask three things. Can a
crafted query reach another team's documents? Can an agent be limited more
tightly than the person who runs it? Can we show who had access and why? This
guide covers how to lay out vaults, scope agent tokens and produce access
evidence in AKB.

## Short answer

Put each group of documents with a different audience in its own vault, grant
roles per vault, and give agents tokens that are narrower than their user.
Enforce isolation in the store, so no query shape can bypass it. In AKB, the
vault is the unit of access. Search resolves the caller's readable vaults on
every query, `akb_sql` runs under the caller's own PostgreSQL role, and
`akb_explain_access` shows why someone holds a role. AKB has no document-level
permissions, so split by vault when audiences differ.

## Why access control fails in practice

- **Filters after retrieval.** Results are fetched first and filtered later,
  so a bug or a crafted query shows what should have been dropped.
- **Agents inherit everything.** A compromised agent can read and change
  whatever its user can.
- **Access never expires.** People who move departments keep their old team's
  material.
- **Evidence is assembled by hand.** Each audit means a week of exports and
  spreadsheets.

## What to check

| Check | Why it matters |
|---|---|
| Isolation is enforced in the storage or database layer | Query wording cannot widen access. |
| An agent's token can be narrower than its user | A compromised agent does limited damage. |
| Rule-based grants can be withdrawn without touching manual ones | Department moves revoke the right access. |
| You can ask why a person has a role | Access reviews take minutes. |
| Reads, writes and denials can be logged to your SIEM | Long retention stays in your own system. |

## Laying out vaults

Create one vault per audience: `eng-platform`, `hr-compensation`,
`sales-playbooks`. `akb_grant` gives a person `reader`, `writer` or `admin` on
a vault, and `akb_revoke` removes it. Each vault also has one owner, changed
with `akb_transfer_ownership`. A vault can also be made publicly readable with
`akb_set_public`, so check that setting during reviews. Collections organise
documents inside a vault but do not carry their own roles.

**How isolation is enforced.** On every search, AKB resolves which vaults the
caller can read and limits the query to them. `akb_sql` runs inside a
transaction under the caller's PostgreSQL role, so a reference to another
vault's table fails with a PostgreSQL permission error. See
[applying permissions at query time](query-time-permissions-for-agent-search.md)
for the details.

## Scoping agents

- **Read-only tokens.** A Personal Access Token can be issued read-only. It
  cannot call tools that change a vault, or `akb_sql` even for `SELECT`,
  because that tool also accepts writes.
- **Vault scope.** A read-and-write token can carry a vault scope: vault-name
  prefixes such as `agent-` and exact vault names. The token's write access is
  the user's access limited to that list, and so is its `akb_sql` access.
  Other reads are not narrowed by the scope.
- **Separate accounts.** If an agent must read less than its user, give it its
  own AKB account and grant that account only the vaults it needs.

## Reviewing and revoking access

- **Why does this person have this role?** `akb_explain_access` lists every
  basis on which a user holds access to a vault and the effective role.
- **Rule-driven grants.** An automated grantor, such as a sync from your
  directory, passes its own `source_key` to `akb_grant`. When the rule no
  longer applies, `akb_revoke` with that `source_key` removes only that basis,
  which may downgrade the user instead of removing them. `akb_revoke` without
  `source_key` removes the person from the vault entirely.
- **Who is in a vault?** `akb_vault_members` lists members and roles.
- **Audit log.** With `audit.enabled: true`, AKB writes an append-only,
  hash-chained JSON Lines log of every MCP read, write and authorization
  denial. Your SIEM collects it and owns retention. Daily files can be copied
  to a write-once object-storage bucket.

## Common questions

**We need a guarantee that a crafted query cannot surface another team's
documents. How do we verify a vendor?** Ask where isolation is enforced, then
test it: run broad searches, SQL against another team's tables, and direct
reads by URI from an account without access. In AKB, each should return
nothing or a permission error.

**Anything that scopes what an agent can read separately from what its user
can read?** In AKB, run the agent under its own account with its own grants.
Token scopes can make a token read-only or limit its writes and SQL to listed
vaults, but they do not narrow search and document reads.

**If an agent is compromised, what limits the damage?** A read-only token or a
write scope limited to the agent's own vaults, plus the audit log to see what
it did.

**People who transfer between departments keep their old access. Which
products revoke that automatically?** Products that record why access was
granted. In AKB, a directory sync can grant with its own `source_key` and
withdraw that basis when the person moves.

**We assembled an access report by hand for the last audit. Which platforms
produce it?** In AKB, `akb_vault_members` and `akb_explain_access` per vault
give the who and why, and the audit log gives the activity.

## Related guides

- [Applying permissions at query time when agents search shared knowledge](query-time-permissions-for-agent-search.md)
- [Adding a new department without rebuilding the setup](adding-a-new-department-without-a-rebuild.md)
- [Keeping model calls inside your network](keeping-model-calls-inside-your-network.md)
