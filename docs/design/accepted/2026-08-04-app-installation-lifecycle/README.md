---
status: accepted
stage: applied
created: 2026-08-04
updated: 2026-08-04
---

# App Installation Lifecycle

## Decision

Expose the existing app desired-state registry through a Vault-keyed HTTP
lifecycle surface. The registry remains the source of truth; the API does not
add a command ledger, idempotency table, migration, worker, or data-plane
operation.

Administrators use:

```text
PUT    /api/v1/apps/{app_id}/installations/{vault_id}
GET    /api/v1/apps/{app_id}/installations/{vault_id}
DELETE /api/v1/apps/{app_id}/installations/{vault_id}
```

The PUT body is `{release_id, capabilities, mode}`, where `mode` is
`install`, `restore`, or `fresh` and defaults to `install`. Capabilities are
deduplicated, sorted, non-empty, and limited to the existing
`SUPPORTED_APP_CAPABILITIES` allowlist.

The app-token read surface is deliberately app-scoped by the token principal:

```text
GET /api/v1/app/installations/{vault_id}
```

There is no app selector on this route. The service rechecks the live
installation and current active grant, and permits only
`installation:read` for `installing`, `active`, `upgrading`, or `blocked`
installations.

## State transitions

- A first install creates one `installing` installation and generation-one
  active grant in one transaction. Identical retries replay the same state and
  return 200; a state-changing command returns 202.
- Commands serialize on the app/Vault pair. Conflicting release or capability
  requests return 409 without partial registry changes.
- Uninstall revokes the active grant, clears desired state, moves owned
  resources to `retained`, and preserves the current release and observation.
  Repeated DELETE is a no-op replay.
- Restore is explicit and requires the retained current release, matching
  observed release, and matching expected/observed schema fingerprints. It
  creates the next grant generation and changes retained resources back to
  owned.
- Fresh is explicit, refuses any retained resource, clears the old current
  pointer, and starts a new `installing` generation only when no resource row
  remains.

## Authorization and projection

System administrators and the target Vault owner/admin may use the
administrator surface, subject to the existing Vault access and PAT-scope
checks. Unauthorized users receive a generic 403 before app, release,
installation, or Vault metadata is resolved. App tokens cannot mutate the
lifecycle and can read only their own live installation with the required
active grant capability.

Status projects installation/app/Vault identifiers, lifecycle, desired/current
and observed release data, grant generations and capabilities, resource
kind/key/status, and sanitized checkpoint/error and release/schema/grant drift.
Credential and token material, grant provenance, worker payloads, and unrelated
Vault metadata are excluded. All lifecycle responses are marked
`Cache-Control: no-store`.

Every accepted, replayed, or denied command emits bounded audit metadata with
the actor, app, installation when known, Vault, generation when known, result,
reason, and correlation ID. Request bodies, capabilities, credentials, tokens,
provenance, and worker payloads are never recorded.

## Compatibility and non-goals

The backend version remains `0.13.0`. Existing registry constraints enforce
installation uniqueness, grant monotonicity, immutable grant identity, and
retained-resource protection. Release registration, upgrades/re-consent,
reconciliation, rollout, SDK generation, frontend/MCP proxy behavior, and
retained-data deletion remain outside this surface.

The governing scope and behavior are the accepted Delivery Contract in the
AKB issue record. This repository document records the implemented public
boundary and is not a second source of product requirements.
