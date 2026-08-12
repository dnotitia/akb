---
status: accepted
stage: applied
created: 2026-08-12
updated: 2026-08-12
---

# PostgreSQL Native Document Revision Backend

## Decision

AKB exposes two stable, process-scoped document revision backends:

```yaml
document_revision_backend: bare_git       # default
# document_revision_backend: postgres_native
```

Both implementations ship in the same backend image and preserve the public
AKB Document logical-revision contract. The unit of compatibility is an AKB
revision—not a Git commit or object graph. Public history, historical get,
message/actor/time/parent metadata, stable selectors, activity, and
create/update/move/delete/recreate diffs remain available through REST and MCP.
Diffs promise stable unified content and correct additions/removals; raw Git
header byte identity is not part of the contract.

`bare_git_current` remains a deprecated alias for `bare_git`.
`native_ledger_m1` remains a guarded measurement alias, requiring its existing
measurement flag and dedicated database name. New deployments must not render
either old name.

## Authority boundary

Choosing `postgres_native` in configuration does not convert a tenant. A new
Native deployment requires an explicit operator command before first startup:

```text
python -m app.cli initialize-postgres-native
```

The command is valid only for a database that has never contained an AKB
schema. Before migrations it creates one singleton bootstrap claim bound to:

- tenant identity;
- deployment namespace;
- provisioned database UUID and current database name;
- initializing OCI image digest.

The operation holds a PostgreSQL advisory lock across schema initialization,
verifies zero Legacy and Native authority facts and no Git refs, then creates a
single pending authority record. First backend startup consumes that record and
creates the immutable durable marker in one transaction. An interrupted
transaction leaves either reusable pending authority or the completed marker,
never an authority gap. Later image versions validate the durable tenant and
database binding; the original image digest remains audit evidence.

An old-but-empty database is ineligible because its AKB sentinel schema exists
without the pre-schema claim. Bare Git startup also rejects a database carrying
any stable Native claim, pending record, or marker. A copied database presented
with a different tenant, namespace, or database UUID is rejected. Detecting an
identical-identity snapshot rollback requires an external provisioning attestor
and is outside this backend contract.

## Scope and non-goals

- Bare Git remains the default.
- Selection is immutable for the backend process; there is no request- or
  Vault-level split and no dual canonical write.
- Existing-tenant activation requires a later, separately approved fenced
  migration record. This implementation mints only `new_database` authority.
- No fleet default, tenant cutover, Bare Git retirement, or production-readiness
  claim is implied by the selector.
