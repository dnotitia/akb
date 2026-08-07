---
status: accepted
stage: applied
created: 2026-08-04
updated: 2026-08-06
---

# App Installation Lifecycle

## Decision

Expose installation lifecycle commands over the existing HTTP API and keep
the registry as the source of truth. The existing app registry, immutable
release rows, monotonic installation grants, owned-resource statuses, and
observed-state tables provide the required storage boundary; no migration is
needed.

## API

System administrators and Vault administrators use:

- `PUT /api/v1/apps/{app_id}/installations/{vault_id}` for `install`,
  `restore`, and `fresh` commands;
- `GET /api/v1/apps/{app_id}/installations/{vault_id}` for an administrator
  projection; and
- `DELETE /api/v1/apps/{app_id}/installations/{vault_id}` for uninstall.

The app principal uses `GET /api/v1/app/installations/{vault_id}`. The app id
comes only from the current app token, and the request requires the live
`installation:read` grant. The route has no app selector.

## Command semantics

Commands validate the exact capability allowlist, serialize on an advisory
lock for the app/Vault pair, and commit the installation row and grant in one
transaction. Exact replays return the stable installation and grant
generations; conflicting release or capability requests return a conflict
without creating partial rows.

Uninstall revokes the active grant, clears the desired release, marks the
installation `uninstalled`, and retains owned resources. Restore is allowed
only when the retained release and observed schema fingerprint match the
requested release. Fresh is allowed only when no retained resources remain;
it clears the old current release and starts the next grant generation in
`installing`.

## Authority and projection

Vault authority is checked before app, release, or installation metadata is
resolved. Non-administrators receive the same denial envelope for existing
and missing identifiers. Public projections contain lifecycle state, release
identity/version, grant generation/capabilities/status, owned-resource
identity/status, and bounded observed-state summaries only. Credential
plaintext, app tokens, grant provenance, issuers, arbitrary worker payloads,
and unrelated Vault metadata are excluded. Lifecycle reads and command
responses use `Cache-Control: no-store`.

## Proof

Pure and route tests cover normalization, redaction, status-code/replay
contracts, app scoping, and error no-store behavior. The live PostgreSQL
integration tests cover concurrent install replay/conflict, atomic uninstall,
compatible and incompatible restore, retained-resource fresh rejection, and
fresh generation progression. The repository-owned E2E runtime also exposes a
resettable `app-installation-lifecycle` fixture scenario with randomized
actors, apps, releases, Vaults, and lifecycle states through sanitized
discovery and log-observation operations.
