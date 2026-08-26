---
status: accepted
stage: accepted
created: 2026-08-26
updated: 2026-08-26
head: 6db98bf
implemented: 1e8c086
method: source reading of `backend/app` at 6db98bf — the `vault_access` schema, both of its writers, every module that reads it in SQL, and the RoleSync catalog — plus the requirements of two independent automated grantors; accepted after implementation against the seven gates and verification of the deployed artifact
---

# Source-Keyed Grant Contributions

This answers the first question of
[#407](https://github.com/dnotitia/akb/issues/407): *can one user have multiple
independent positive access bases for the same resource without one writer
overwriting another?*

It deliberately does **not** answer #407's other questions. Resource hierarchy
(Vault → Collection → Document) and coverage of Files and Tables are separate
decisions, and nothing here requires or presumes them. The scope stays at the
Vault grain AKB already enforces.

## 0. One-page summary

> AKB stores the **result** of a grant, not the grant. `vault_access` holds one
> row per `(vault_id, user_id)`, `grant` overwrites its `role`, and `revoke`
> deletes the row. The only provenance recorded is `granted_by` — *who*, never
> *why*. That is coherent while every grant is a person acting on another
> person, because then there is only ever one reason. It stops being coherent
> the moment a second, automated grantor exists, because two reasons then
> compete for one cell and the loser is destroyed silently.
>
> The proposal is to record each **contribution** separately — a role a named
> source contributes to one user on one vault — and to keep `vault_access` as
> the **materialized effective row** derived from them. Removing a contribution
> then removes exactly its own effect and nothing else.
>
> This is deliberately the smallest change that makes that true. Every read
> path, every enforcement surface and PostgreSQL RoleSync keep reading the row
> they read today.

## 1. What is missing, exactly

At `6db98bf`:

```sql
-- backend/app/db/init.sql
CREATE TABLE IF NOT EXISTS vault_access (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    vault_id UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'reader',
    granted_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(vault_id, user_id)
);
```

```python
# access_service.grant_access
INSERT INTO vault_access (id, vault_id, user_id, role, granted_by)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (vault_id, user_id)
DO UPDATE SET role = $4, granted_by = $5     # overwrite, not maximum

# access_service.revoke_access
DELETE FROM vault_access WHERE vault_id = $1 AND user_id = $2
```

`granted_by` is the actor, and it is a single value. `role_source`, returned by
`check_vault_access`, classifies which authorization *path* allowed the current
request (`member`, `public`, `system_admin`, `write_policy_grant`,
`write_policy_admin_bypass`); it is computed per request, is not stored on the
row, and does not distinguish one member basis from another.

So the failure is not a bug in either function. Both are correct for the model
they implement. The model has no place to put a second reason.

### The failure, concretely

1. A person holds `reader` on vault `V`, granted directly.
2. An automated grantor applies `writer` on `V` for everyone in some set the
   person belongs to. The row becomes `writer`. The direct `reader` no longer
   exists anywhere.
3. The person leaves the set. The grantor revokes. The row is deleted.

The person now has no access to `V`, though nobody ever revoked the access they
held for their own reasons. There is no query that could have prevented this,
because after step 2 the information needed in step 3 is gone.

## 2. Two consumers, one primitive

Two independent automated grantors in this ecosystem need the same thing, which
is the evidence that this is a primitive rather than one feature's requirement:

| Consumer | Rule it holds | Why it must revoke |
| --- | --- | --- |
| A workspace administration surface | "this set of people has this role on this vault", following the set's membership | somebody leaves the set |
| A derivation policy that writes into a third vault | "the target's readers are those who can read every declared source" | somebody loses read on a source |

Neither can be implemented correctly today, and both fail in the same way: the
revoke deletes access that was never theirs to remove. Neither consumer needs
AKB to understand *what* their rule is — only to keep the contributions apart.

## 3. What this proposal does not decide

- **It does not put the consumers' concepts into AKB Core.** AKB gains no notion
  of a group, a team, a policy or a derivation. It learns only that a
  contribution carries a source key it never interprets.
- **It does not decide who runs a reconciler**, or with what authority. An
  automated grantor still needs an identity, and choosing that identity is the
  consumer's decision, not this contract's.
- **It does not change the grain.** Collection- and Document-scoped access
  remain out of scope, as does anything about Files or Tables.
- **It does not touch the non-member authorization paths.** Ownership, public
  access, system administration and the vault write policy are decided before
  and beside member ACL today, and stay exactly where they are — see §4.6.

## 4. Decisions, with recommendations

Each of these is a real choice. The recommendation is given with the evidence
that produced it, so a different decision can be made by disputing the evidence
rather than the preference.

### 4.1 Where the effective value lives

**Options.** (A) Keep `vault_access` as the materialized effective row and add a
contribution store behind it, recomputed in the same transaction. (B) Replace
`vault_access` with contributions and have every reader compute the effective
role.

**Recommendation: A.**

**Grounds — measured at `6db98bf`.** Outside `access_service`, `vault_access`
is read in SQL by five modules at eleven sites:

| Module | Sites | What it decides |
| --- | --- | --- |
| `services/search_service.py` | 4 | which vaults a search may see |
| `services/m1_native_grep_service.py` | 2 | which vaults grep may see |
| `services/revision_backend.py`, `repositories/native_revision_repo.py` | 2 | revision visibility |
| `services/role_sync.py` | 3 | **the catalog PostgreSQL role membership is reconciled from** |

`access_service` itself adds the authorization chokepoint, member listing and
counts; `document_service` deletes the rows when a vault is deleted.

Under (A) none of these change: they keep reading the same row with the same
meaning. Under (B) all of them change, and each one is an opportunity for a
visibility defect in a different direction — a document surfacing in search from
a vault the caller cannot open, or disappearing from grep for one they can.

The RoleSync line is the decisive one. `role_sync` reconciles PostgreSQL group
membership from literally `SELECT vault_id, user_id, role FROM vault_access`.
Under (A) the PostgreSQL enforcement layer needs no change at all. Under (B) it
has to learn the derivation too, and a divergence between the application check
and the database role is the one class of authorization bug that cannot be seen
from the application.

There is also a shape argument. AKB **already** runs this pattern one layer
down: `grant_access` writes the catalog row and then calls
`role_sync.on_grant`, whose own comment reads *"Best-effort — reconciler covers
drift"*, with `/admin/role-state` exposing the diff and `/admin/reconcile-roles`
applying it. (A) adds a third layer above the catalog in the shape AKB already
has, instead of introducing a second, different one.

### 4.2 The shape of the source key

**Options.** (A) One opaque `TEXT`, with `direct` reserved. (B) A two-part
`(kind, id)` where `kind` comes from a registry AKB validates against.

**Recommendation: A — one `TEXT`, `direct` reserved, validated only for shape.**

**Grounds.** AKB must not interpret the key; the moment it does, it has imported
the consumer's concept, which §3 rules out. A registry would only prevent two
independent grantors choosing the same key — an operator configuration concern,
handled the same way clashing client identifiers are handled elsewhere.

The decisive point is reversibility: a registry can be added later as a
*validation* without changing the row shape, whereas splitting a single column
into two later is a migration. Take the cheap option precisely because it is the
one that can be revised.

Recommended convention, not enforcement: `<namespace>:<opaque id>`. `direct` is
reserved for a contribution made by a person acting directly, and is the value
every existing row migrates to (§4.5).

### 4.3 What revoking a contribution means

**Options.** (A) Remove that contribution and recompute; if others remain the
user is *downgraded*, and only the last removal deletes the row. (B) Additionally
undo derived effects retroactively.

**Recommendation: A, and A only.**

**Grounds.** (A) is exactly the missing property and nothing more: *"remove one
basis and only its own contribution goes"*. (B) is a different feature — it asks
whether access already exercised should be walked back, which depends on what
the consumer derived and is not a property of the grant store. Answering it here
would make the smallest contract not small.

The consequence to state plainly in the API: revoking a contribution can leave a
user with **less** access and can also leave them with the **same** access, and
a caller must not read "revoked" as "the user can no longer reach this vault".

### 4.4 What "applied" means

**Options.** (A) The catalog commit is *applied*; PostgreSQL role convergence is
a separate, separately observable state. (B) A write is not applied until roles
have converged.

**Recommendation: A, with the second state made explicit in the response.**

**Grounds.** (A) is already what AKB does — `on_grant` is best-effort and the
reconciler covers drift — so (B) would be a behaviour change to the existing
direct-grant path, not just to contributions. But the two states must not be
collapsed in what the API *says*, because the layer that actually gates
`akb_sql` is the PostgreSQL role. A caller told "applied" on catalog commit,
with no way to observe convergence, will report success for something a person
still cannot do.

So: keep the semantics, name both states, and let a caller that needs to wait,
wait.

### 4.5 Existing rows

**Recommendation.** Backfill every existing `vault_access` row as one
contribution with source key `direct`, preserving `role`, `granted_by` and
`created_at`. The effective row is unchanged by definition, so the migration is
invisible to every reader — which is only possible under §4.1(A), and is one
more reason to take it.

Ownership transfer needs one note: `transfer_ownership` writes a `vault_access`
row for the previous owner and deletes the new owner's. Under this proposal it
writes a `direct` contribution and removes the new owner's contributions;
ownership itself stays where it is, outside the contribution plane (§4.6).

### 4.6 The non-member paths stay outside

**Recommendation.** Ownership, `public_access`, system administration and the
vault write policy are **not** contributions and do not enter the derivation.

**Grounds.** `check_vault_access` already decides them on separate branches and
already reports them separately through `role_source`. Folding them in would
create states the model cannot express — "revoke the ownership contribution" is
not a sentence — and would put the break-glass and public-access rules behind a
derivation they have no reason to be behind.

### 4.7 Who may write a contribution

**Recommendation.** Unchanged: whatever authority `grant`/`revoke` require
today. A source key is a label on a grant, not a new authority axis.

**Grounds.** Keeping authority unchanged is what keeps this contract "smallest",
and the interesting authority question — what identity an automated grantor
should hold — belongs to the consumer that runs one, not to the store. Settling
it here would settle it for every future consumer by accident.

## 5. Sketch — non-binding

Names, types and endpoints are for discussion; the semantics above are the
proposal.

```sql
CREATE TABLE vault_access_contributions (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    vault_id     UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role         TEXT NOT NULL,
    source_key   TEXT NOT NULL,          -- 'direct', or '<namespace>:<opaque>'
    granted_by   UUID REFERENCES users(id),
    revision     BIGINT NOT NULL,        -- monotonic per (vault, user, source)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (vault_id, user_id, source_key)
);
```

- `vault_access` keeps its shape and becomes the materialization of
  `max(role)` over the contributions for that pair, written in the same
  transaction.
- `revision` is what makes a retrying automated grantor safe: a write carrying
  an older revision than the stored one is a no-op, not an overwrite.
- Grant and revoke take an optional source key defaulting to `direct`, so every
  existing caller keeps its exact current behaviour.
- Readback returns the effective role **and** the contributions that produced
  it, which is what lets a caller verify its own basis without being shown
  anyone else's business.

## 6. Acceptance gates, written before implementation

Written now, and expected to be red until the change lands. A gate that cannot
fail proves nothing.

1. **Independent bases.** direct `reader` + source `writer` ⇒ effective
   `writer`; remove the source contribution ⇒ effective `reader`, **not** no
   access. This case is impossible to satisfy at `6db98bf` and is the whole
   point.
2. **Last removal.** Removing the only remaining contribution removes the row.
3. **Two automated sources.** Two different source keys on the same pair;
   removing one leaves the other's role intact.
4. **Replay.** Applying a stale revision after a newer one changes nothing.
5. **Every read surface agrees.** REST, `akb_sql`, search, grep, revision
   visibility and PostgreSQL role membership give the same answer for the same
   pair, including immediately after a downgrade.
6. **No silent widening.** A contribution can never grant more than the caller
   could have granted directly.
7. **Mutation control.** Each gate is verified to fail when the corresponding
   guard is removed. A suite that stays green under mutation is not evidence.

## 7. Open, and deliberately left open

- The exact table and endpoint names, and whether contributions are exposed on
  the existing access endpoints or their own.
- Whether readback shows a caller contributions they did not create.
- How a consumer observes PostgreSQL convergence (§4.4) — a status field, an
  event, or reuse of the existing role-state diff.
- Everything in #407's second and third questions, which this proposal does not
  touch.

## 8. Accepted, and what acceptance rests on

Direction settled on 4.1(A) and implemented in `1e8c086`, with the PostgreSQL
half of gate 5 added in `95e2b233`. The two corrections that acceptance had to
absorb are recorded in `feedback/`; the public rounds are in `rounds/`.

**What was built.** `vault_access_contributions` behind an unchanged
`vault_access`, recomputed in the caller's transaction; one derivation function;
an optional source key on grant and revoke defaulting to `direct`; a backfill
that the migration refuses to commit if any pair's derived role differs from its
stored one; the basis and both effective roles on the access events; and an
explanation endpoint that reports the bases plus the non-member paths.
`role_sync.py` is unchanged, which was half the reason for choosing (A).

**Two contract points the implementation settled**, both of which amend how §4.3
and §4.7 should be read:

- An administrator's revoke — the one with no source key — removes **every**
  basis. §4.3 read literally would have removed only `direct`, which would let
  the button report success while the person kept access a rule had given them.
  §4.3's warning belongs to the source-keyed form, where a surviving basis makes
  the operation a downgrade rather than a removal.
- The PostgreSQL layer is told the **effective** role, not the requested one, in
  both directions. Granting `reader` to somebody a rule already made a `writer`
  must not demote them in the database, and a surviving basis after a revoke is
  a downgrade rather than a removal.

**What acceptance rests on.** The seven gates of §6 as executable tests against
live PostgreSQL, each verified to turn red when the guard it names is removed.
Two of them use the real `RoleSync` against a real server, because asserting
what the hook was *told* cannot see whether the database agreed — and `akb_sql`
is gated by group membership, not by `vault_access`.

Beyond the repository, the deployed artifact was exercised in a disposable
environment: the migration chain at real startup, the HTTP contract, the event
stream, and the enforcement chain end to end — a reader's write refused, the
same member promoted through a rule basis writing once, and the write refused
again after that basis is withdrawn while reads still succeed. Run against the
commit immediately before this change, that same lane reproduces the original
defect: the downgraded member loses the direct `reader` they still held.

**Still open.** Everything in §7, unchanged. In particular the subject question
— user or set — remains deliberately undecided, and contributions are the
substrate either way.
