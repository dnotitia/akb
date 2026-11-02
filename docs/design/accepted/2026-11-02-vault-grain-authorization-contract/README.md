---
status: accepted
stage: accepted
created: 2026-11-02
updated: 2026-11-02
head: 4698900
method: documentation of current behaviour. `backend/app` and `backend/mcp_server` were read at 4698900, and the PostgreSQL suites named in §11 were run against that head. No code, schema, permission or operational change.
---

# Vault-Grain Authorization Contract

This closes [#407](https://github.com/dnotitia/akb/issues/407), which asked AKB
to define its authorization model before building finer-grained access. The
decision has three parts:

- the Vault stays the only resource that carries policy;
- Collection, Document and Table-row grains are deferred, and each one has a
  condition that reopens it;
- the contract AKB enforces today is written down, so a future slice can start
  from a stated baseline instead of from reading the code.

The repository owner, who opened #407, chose this direction on 2026-11-02.
Nothing here changes code, schema, permissions or operations. Every statement
about behaviour cites a `path:line` at `4698900`. Line numbers will drift, and
the symbol names give a second way to find each site.

#407's first question, about independent access bases, was answered by
[Source-Keyed Grant Contributions](../2026-08-26-source-keyed-grant-contributions/README.md)
(below, "ADR-0826"). This record does not reopen that answer. §1 lists the two
factual corrections it makes to ADR-0826.

## 0. Summary

> AKB authorizes at one grain. The principal is a user. A credential the user
> presents can narrow what that user may do but never widens it. The Vault is
> the only resource that carries policy. Collections, Documents, Files, Tables,
> assets, relations, graph views, search results, counts and activity all take
> the Vault decision. Publication links are a separate path based on a
> capability link.
>
> A user's role on a Vault is the stronger of two values: their effective
> member role, which is the strongest of their contributions (ADR-0826), and
> the Vault's public level. Some paths decide before that comparison: the
> archive and mirror guards, the credential's Vault scope, the write policy,
> system administration and literal ownership.
>
> Grant, revoke and ownership transfer commit the catalog change, the
> PostgreSQL membership change and the outbox event in one transaction, or
> commit none of them.
>
> Finer grains, set subjects, deny semantics and Core enforcement of
> derived-resource audiences are deferred (§7). A slice that reopens any of
> them must first answer §8.

## 1. Relationship to ADR-0826

ADR-0826 remains the record for independent access bases: positive,
source-keyed contributions; `max` as the derivation; a revoke without a source
key removes every basis. This record adds two dated amendments to it:

1. **Subject (D1).** ADR-0826 §8 said the subject question (user or set) was
   still undecided. It was decided on 2026-08-27, in
   [the last comment on #407](https://github.com/dnotitia/akb/issues/407#issuecomment-5435843810):
   the subject is a **user**, and AKB Core has no native group subject. §2
   below records that decision.
2. **What "applied" means for grant, revoke and transfer (D2).** ADR-0826 §4.4
   and §7 describe the behaviour measured at `6db98bf`. There, the catalog
   commit was "applied", and PostgreSQL role convergence was a separate state
   that a best-effort hook reached. Commit `d6ed12b` (#696) changed that for
   these three operations. They now apply the PostgreSQL membership inside the
   catalog transaction and abort if it fails (§6). The amendment covers grant,
   revoke and transfer only.

## 2. Principal

- **The subject is a user id.** `vault_access` and
  `vault_access_contributions` are keyed by `(vault_id, user_id)`
  (`backend/app/db/init.sql:429`, `:448`). There is no set, group or team
  subject. A grantor that works from a set writes one contribution per user,
  under its own source key (ADR-0826 §4.2). The decision and its reasoning are
  in the issue comment linked in §1.
- **Service accounts are users.** They go through the same paths. One thing
  is reserved for them: an action-limited write grant. It requires a non-admin,
  active service account whose unexpired service key has exactly the coarse
  `write` scope and a Vault scope of exactly the target Vault
  (`backend/app/services/access_service.py:170-206`). The action vocabulary is
  `*` and `file_upload` (`access_service.py:96-98`).
- **Credentials only narrow.** Three checks apply:
  - Coarse token scopes are checked once per request, before any Vault check
    (`backend/app/api/deps.py:193-203`).
  - A token's Vault scope limits **mutations** only. A `writer`, `admin` or
    `owner` request (`_MUTATING_ROLES`, `access_service.py:55`) on a Vault
    outside the scope is refused (`check_vault_scope`,
    `access_service.py:245-277`). This check runs before the system-admin and
    owner paths (`access_service.py:391`), so a scoped administrator token
    cannot escape its scope.
  - A scoped system-admin token does not get the write-policy bypass
    (`_is_unscoped_system_admin`, `access_service.py:229-242`).

## 3. Resource and grain

The **Vault** is the only resource that carries policy. Every other resource
gets its decision from `check_vault_access` on its Vault:

| Resource | How it inherits the Vault decision |
| --- | --- |
| Collections | browse `reader`, create and delete `writer` (`backend/app/api/routes/collections.py:84`, `:107`, `:147`) |
| Documents | get `reader`; put, move, update and delete `writer` (`backend/app/api/routes/documents.py:143`, `:103`, `:119`, `:161`, `:179`) |
| Files | list, metadata, placements and download `reader`; delete `writer` (`backend/app/api/routes/files.py:502`, `:515`, `:546`, `:560`, `:570`); upload `writer` with write action `file_upload` (`backend/app/api/file_write_context.py:21-26`) |
| Document images (assets) | policy, metadata and read `reader` (`backend/app/api/routes/assets.py:98`, `:150`, `:288`); upload and discard through the same writer context as Files (`assets.py:51`, `:358`) |
| Tables | schema and read `reader`; rows `writer`; create and migrate `writer`; alter and drop `admin` (`backend/app/api/routes/tables.py`, §4) |
| Relations, graph, provenance | read `reader`, write `writer` (`backend/app/api/routes/knowledge.py:143`, `:165`, `:192`, `:236`, `:264`, `:288`, `:308`) |
| Activity | `reader` (`backend/app/api/routes/activity.py:66`, `:117`, `:142`, `:173`) |
| Search | filtered to readable Vaults: owner, member row, or public (`_accessible_vault_ids`, `backend/app/services/search_service.py:1399-1430`) |
| Counts, Vault info, member list | `reader` (`access_service.py:1023`, `:788`) |
| `akb_sql` | PostgreSQL role membership derived from the member row, the owner and the public level (`backend/app/services/role_sync.py:34-37`), enforced by the database (`backend/app/services/user_sql_executor.py`; `docs/designs/pg-native-rbac/00-overview.md`) |

No Collection, Document, File or Table has a policy of its own. The search
design says the same (`docs/designs/search-acl-scale/00-overview.md:29-31`), and
so does the published guidance (`docs/guides/query-time-permissions-for-agent-search.md:76-77`,
`:122-124`).

**Publications are a separate path.** A publication is a capability link that
serves an item without the reader logging in. Creating or deleting one is
checked against the Vault (§4). Reading one is not a Vault decision, and the
guidance says so (`docs/guides/query-time-permissions-for-agent-search.md:85-87`).
This record does not merge the two paths.

## 4. Actions

The current action-to-role matrix, as implemented. The role column is the
minimum `required_role` passed to `check_vault_access`. A requirement of
`owner` is met by the literal owner, by a system administrator (role `owner`,
`access_service.py:450-457`), and by the write-policy admin bypass
(`access_service.py:421-437`).

| Action | Minimum | Where |
| --- | --- | --- |
| Read, list, browse, graph, activity, member list, Vault info | `reader` | §3 rows |
| Create, update, move or delete documents, collections and relations | `writer` | §3 rows |
| Insert, update or delete table rows; a write-shaped table query | `writer` | `tables.py:484`, `:519`, `:553`, `:591` |
| Create a table; apply a table migration | `writer` | `tables.py:294`, `:343` |
| Alter a table; drop a table | `admin` | `tables.py:382`, `:668` |
| Import into a Vault | `writer` | `backend/app/api/routes/knowledge_io.py:71` |
| Create a publication, delete a publication, create a snapshot | `writer` | `backend/app/api/routes/public.py:312` (plus each queried Vault for a table query, `:316-320`), `:384`, `:459`; MCP `backend/mcp_server/server.py:1330`, `:1338` |
| Grant or revoke a member | `admin` | `access_service.py:590`, `:686`, re-checked under the Vault row lock at `:603-617`, `:704-717` |
| Explain access | `reader` for yourself, `admin` for another user | `access_service.py:872` |
| Update the Vault skill document | `owner` | `documents.py:163-165` |
| Update Vault metadata, including its public level | `owner` | `access_service.py:1495`, `:1505` |
| Set public access | `owner` | `access_service.py:1535` |
| Archive, unarchive, transfer ownership | `owner` | `access_service.py:1443`, `:1460`, `:1247` |
| Delete a Vault (also when archived) | `admin` | `access_service.py:1993-1995` |
| Create a Vault | Vault-scope check as `owner` (no row exists yet) | `documents.py:60-63` |
| Mark a Vault write-managed; add or remove write grants | system administrator | `backend/app/api/routes/access.py:446-470` (`_require_admin`, `:68`) |

This matrix is in code only. No product surface presents it as one table.

## 5. Bases and the effective decision

`_check_vault_access_impl` (`access_service.py:317-482`) evaluates in this
order. The first step that decides wins.

1. **Unknown Vault:** `NotFoundError` (`:345-347`).
2. **Archived:** a `writer` or `admin` request is refused unless the caller
   passed `allow_archived` (`:359-364`). This runs before every bypass.
3. **External Git mirror:** a `writer` request is refused (`:369-377`).
4. **Credential Vault scope:** a mutating request outside the scope is refused
   (`:391`, §2).
5. **Write policy:** on a write-managed Vault, a mutating request succeeds
   only in one of two cases. Either the caller's token is on the grant list
   with a matching action (`role_source` `write_policy_grant`, `:400-420`), or
   the caller is an unscoped system administrator, which is recorded as a
   `vault.write_policy_admin_bypass` event (`:421-437`). Every other caller is
   refused, including the owner (`:438-443`).
6. **System administrator:** role `owner`, `role_source` `system_admin`
   (`:450-457`).
7. **Literal owner:** role `owner`, `role_source` `member` (`:460-461`).
8. **Member and public:** the stronger of the `vault_access` role and the
   Vault's `public_access` (`:465-482`). The result is refused with
   `ForbiddenError` if it is below the requirement (`:476`).

Within step 8, the member role is the materialized `max` over positive
contributions (`effective_role`,
`backend/app/services/access_contributions.py:69-82`). Ownership, public
access, system administration and the write policy are not contributions
(ADR-0826 §4.6). The explanation reports them separately (§6). A system
administrator is not filtered by search
(`docs/guides/query-time-permissions-for-agent-search.md:83-84`).

## 6. Mutation success, explanation, events and audit

**Grant, revoke and transfer.** Each runs in one transaction under
`role_authority_transaction`, which is a READ COMMITTED transaction holding a
shared advisory lock (`backend/app/services/role_authority.py:11-22`). Inside
it, the operation does four things:

- writes the catalog;
- recomputes contributions;
- writes the outbox event on the same connection
  (`backend/app/repositories/events_repo.py:20`, `:59`);
- projects PostgreSQL membership with `sync_vault_user_in_conn`
  (`backend/app/services/role_sync.py:592-609`).

The sites are `access_service.py:594`/`:651` (grant), `:695`/`:762` (revoke),
and `:1259`/`:1310-1311` (transfer). If the projection cannot reach the
catalog's answer, it raises (`role_sync.py:1492-1501`, `:1512-1515`), the
transaction aborts, and the catalog change does not commit. For these three
operations, a success response means the catalog, the PostgreSQL membership
and the event committed together. ADR-0826's "separately observable
convergence" state no longer exists for them. This record does not
characterise any other mutation.

The grant response carries the requested `role` and the resulting
`effective_role`. The two differ when a stronger basis already exists.
`applied` is false when the call replays a stale revision
(`access_service.py:654-665`).

**Explanation.** `explain_vault_access` (`access_service.py:842-922`) returns
four fields:

- `effective_role`, the stored row;
- `derived_role`, recomputed from contributions;
- every contribution, with its source key, revision, grantor and timestamps;
- `non_member_paths`: owner, system administrator, public level and write
  policy.

`effective_role` and `derived_role` are reported separately, so a broken
recompute is visible instead of hidden.

**Events are not authority.** Event rows are written in the same transaction
as the domain change. A publisher drains them to a stream, and PostgreSQL
remains the source of truth (`backend/app/services/events_publisher.py:1-9`).
A consumer that needs the authorization answer reads it from AKB, not from the
stream.

**Reconcile.** A reconciler rebuilds PostgreSQL role state from the catalog.
It runs at startup (`backend/app/services/lifecycle.py:282`), hourly by
default (`role_sync_reconcile_interval_secs = 3600`,
`backend/app/config.py:1370`; `lifecycle.py:450-457`), and on demand.

**Audit.** The audit log is opt-in (`audit.enabled` defaults to false,
`config.py:278`). It is a best-effort, hash-chained append written after the
operation, outside the transaction (`backend/app/services/audit_log.py:1-19`).
REST grant and revoke go through the same producer
(`backend/app/api/routes/access.py:312-319`).

## 7. Deferred, with reopen triggers

Nothing below is rejected. Each item stays closed until its trigger is met,
and a slice that reopens it must first answer §8.

| Item | Today | Reopen trigger |
| --- | --- | --- |
| Collection-grained policy | Collections take the Vault decision (§3) | A concrete case where material with a different audience must stay in the same Vault and a separate Vault is shown not to work, plus an accepted ADR that answers §8 |
| Document-grained policy | Documents take the Vault decision (§3) | The same trigger, at Document grain |
| Table row-level policy | A Table takes its Vault's decision for every row. The guidance is to keep sources with different audiences in different Vaults (`docs/guides/query-time-permissions-for-agent-search.md:78-82`) | A Table that must hold rows with different source audiences in one Vault, plus an accepted ADR that answers §8 for rows, including SQL and search |
| Set (group) subjects | User subject only (§2) | Evidence that per-user contributions are not enough. The 2026-08-27 comment names the signals to watch: partial application by a rule-driven consumer, or a consumer read path that per-user contributions cannot serve after the consumer-side fixes named there |
| Deny, negative or suppression bases | Positive contributions only. `max` is the whole derivation | A concrete use case that positive bases cannot express. ADR-0826 §8 already defers a suppression basis until a per-person exception is requested |
| Core enforcement of derived-resource audience | Not enforced (§9) | A Core feature that itself derives content across Vaults, or an accepted ADR that moves this enforcement from consumers into Core |

## 8. Entry conditions for a finer-grained slice

A slice that reopens an item in §7 must answer these questions in its own ADR
before implementation. This record deliberately does not answer them.

1. **Hierarchy.** Does a parent decision act as a default, a ceiling, or both?
   May child policy only add access, or may it also narrow it?
2. **Discovery.** If a caller can read a child but not its parent or siblings,
   what container metadata may it see? The current existence signal is in §9.
3. **Actions.** Which actions from §4 split at the new grain? In particular,
   does write authority at that grain include authority to widen the audience
   (publication, public access)?
4. **Coverage.** Which of Files, Tables, assets, relations, graph views,
   search, counts and activity carry their own policy at the new grain, and
   which inherit it?
5. **Compatibility.** What happens to existing Vault grant and revoke clients,
   REST, MCP and generated clients?

A slice must also keep these invariants, which already hold today or are
already accepted:

- Every surface in §10 gives the same answer for the same principal and
  resource, including immediately after a downgrade or revoke (ADR-0826
  gate 5).
- A basis never grants more than its grantor could grant directly (ADR-0826
  gate 6).
- A credential only narrows (§2).
- Grant, revoke and transfer keep their single-transaction guarantee (§6).
- Each new guard is shown to fail when it is removed (ADR-0826 gate 7).

## 9. Current facts recorded, not changed

These describe AKB as it is at `4698900`. Recording them here does not
endorse them, extend them, or add exceptions to them. Changing any of them is
out of scope for this record and needs its own decision.

1. **A writer can create and delete Publications, but only the owner can set
   public access.** Publication create and delete require `writer`
   (`public.py:312`, `:384`; MCP `server.py:1330`). `set_public_access`
   requires `owner` (`access_service.py:1535`).
2. **A Vault admin can delete a Vault, but only the owner can archive,
   unarchive or transfer it.** `delete_vault` requires `admin`
   (`access_service.py:1993-1995`). Archive, unarchive and transfer require
   `owner` (`access_service.py:1443`, `:1460`, `:1247`).
3. **Unknown and forbidden Vaults get different responses.** An unknown Vault
   name raises `NotFoundError` (`access_service.py:345-347`). REST returns it
   as HTTP 404 (`backend/app/exceptions.py:45-47`;
   `backend/app/main.py:179-204`) and MCP as `not_found`
   (`backend/app/util/errors.py:192-193`). An existing Vault that refuses the
   caller raises `ForbiddenError` (`access_service.py:476`, and the guards at
   `:364`, `:375`, `:391`, `:439`). REST returns that as HTTP 403
   (`exceptions.py:149-161`) and MCP as `permission_denied`
   (`errors.py:190-191`). The difference tells an authenticated caller whether
   a Vault name exists.
4. **Core does not enforce derived-resource audience.** Core write paths
   authorize against the target Vault only (for example `documents.py:103`).
   No Core path was found that compares the audience of a write's inputs with
   the target Vault's audience. A search of `backend/app` for `audience` finds
   token and OAuth audience handling plus two docstrings, and nothing else.
   This is the result of a search, not an exhaustive proof. Consumers that
   derive content own this (ADR-0826 §2).

## 10. Surfaces that must agree

At `4698900`, a search of `backend/app` outside migrations for SQL that
selects from, joins, inserts into or updates `vault_access`
(`(FROM|JOIN|INTO|UPDATE) +vault_access`) finds these modules:

- `services/access_service.py` (12 sites)
- `services/search_service.py` (4)
- `services/m1_native_grep_service.py` (3)
- `services/role_sync.py` (3)
- `services/access_contributions.py` (3)
- `services/revision_backend.py` (1)
- `repositories/native_revision_repo.py` (1)
- `services/document_service.py` (1)
- `services/account_self_service.py` (1)
- `services/app_installation_service.py` (1)
- `services/notification_service.py` (1)

These must agree with one another, and also with:

- the REST routes and the MCP handlers, which reach the same service
  functions (for example `server.py:1493` grant and `:1669` set-public);
- PostgreSQL role membership and `akb_sql`;
- the event payloads (`access.grant`, `access.revoke`,
  `access.transfer_ownership`);
- the explanation endpoint;
- the OpenAPI contract and generated clients.

## 11. Conformance

**Existing tests mapped to this contract:**

| Behaviour | Test |
| --- | --- |
| Independent bases, last removal, two sources, stale replay | `backend/tests/test_access_contributions_postgres.py:202`, `:233`, `:253`, `:277` |
| Read surfaces agree after a downgrade; PostgreSQL never told to demote below the effective role | same file, `:310`, `:364` |
| A source key grants no authority; ownership is not a contribution; an administrator's revoke removes every basis | same file, `:385`, `:408`, `:427` |
| Events carry the basis and both effective roles | same file, `:571`, `:591` |
| The explanation lists bases and non-member paths, gates other users at `admin`, and reports recompute drift | same file, `:622`, `:649`, `:677`, `:696` |
| PostgreSQL membership matches the effective role when grant and revoke return | same file, `:795`, `:829` (live `RoleSync`) |
| A projection that cannot converge rolls back the catalog change; noncanonical options are refused; a delayed callback cannot reinstate access | `backend/tests/test_role_membership_delta_pg.py:153`, `:177`, `:191`, `:208`, `:139` |
| Credential Vault scope | `backend/tests/test_vault_scope_unit.py` |
| Write policy | `backend/tests/test_vault_write_policy_unit.py`, `backend/tests/test_vault_write_policy_routes_unit.py` |
| System administrator versus owner semantics | `backend/tests/test_admin_ownership_semantics_unit.py` |
| `akb_sql` isolation by PostgreSQL ACL | `backend/tests/test_pg_rbac_e2e.sh` |

**Run for this record.** Both suites ran against a private PostgreSQL with the
schema built by `init_db`:

- `test_role_membership_delta_pg.py`: 15 passed and 1 skipped. The skipped
  test is the full role-prune oracle, which needs an exclusively owned cluster
  and is unrelated to §6.
- `test_access_contributions_postgres.py`: errors in setup for all 23 tests
  at `4698900`. Its `_MIGRATION_HEAD` pin names
  `113_bm25_vocab_epoch.py`, while the registry now ends at
  `126_platform_service_bootstrap.py`. With only that module constant rebound
  to the registry's last migration, outside the repository, all 23 pass with
  0 skipped. Updating the pin is a test change and is outside this
  documentation-only record.

Transfer's rollback is supported by reading the code only
(`access_service.py:1259-1311`). No PostgreSQL test located here drives
transfer through a projection failure.

**Cases not mapped to an existing test.** None was located during this
documentation pass. Adding them is future work:

- the full §4 matrix, asserted action by action at each role;
- the §9 facts as characterization tests, so that a change to them is
  deliberate;
- transfer under a projection failure;
- REST and MCP parity for the not-found and forbidden responses.

## 12. Non-goals

- Any change to code, schema, permissions, routes or operations.
- Choosing a hierarchy, default-versus-ceiling semantics, or a discovery rule
  for a finer grain (§8 leaves these to that slice's ADR).
- Group or set subjects, deny semantics, expiry, or new capabilities.
- Endorsing or changing the facts in §9.
- Freezing route names or the `vault_access` table as the future
  representation.

## 13. Consequences

- The question "can AKB restrict this one document?" has a written answer:
  no. Material with a different audience goes in its own Vault, and §7 says
  what would change that.
- A future slice starts from §4, §5 and §8 instead of rediscovering them from
  code. Any difference between this record and the code at a later head is a
  defect in one of the two, and the slice should resolve it.
- The §9 facts are now visible as decisions someone could make, not
  accidents someone might trip over. None of them changes until its own
  decision is made.
