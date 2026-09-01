# Generic app control-plane and typed SDK — Design

**Status**: candidate implementation

## Boundary

The app control plane is server-owned state. The REST API owns app definitions,
immutable releases, credentials, installation lifecycle, inventory, snapshots,
and rollout ledgers; the SDK is a typed HTTP caller and keeps no registry or
rollout cache. Data-plane calls remain in the root and `./lite` entrypoints.
Control-plane calls are available only from `@akb/client/control-plane`.

Admin and app clients accept different token configuration fields. A deployment
credential is accepted only by `exchangeAppCredential`; its raw value is sent
once in the exchange request and is not returned or retained by the facade.

## REST and OpenAPI

`app_registry` owns the generic system-admin registry routes. Existing identity,
installation, inventory, and rollout routes use the shared models in
`app/api/control_plane_models.py`. The OpenAPI hook supplies the canonical
`AkbError` response for control-plane failures and stable camelCase operation
IDs. The checked-in control-plane fixture is compared with `app.openapi()` so
route status, request, success, and error references cannot drift silently.

## App Release Manifest v2

Release registration accepts one strict, immutable v2 manifest. It contains
`manifest_version: 2`, the immutable `app_key`, full `source_revision`, an
immutable OCI `image_digest`, `schema_version`, and a complete `schema`
projection. The projection contains every table's canonical `name`, `columns`,
`unique_keys`, and `indexes`, plus its derived `fingerprint`. The manifest also
contains `transition_plans`: exactly one `fresh` plan and zero or more plans
whose source is the exact `{release_version, schema_fingerprint}` pair.

Steps use only bounded structured operations. A fresh `create_table` step must
carry the complete final table descriptor, including required column flags,
unique keys, and indexes. Raw SQL, expressions, custom code, and destructive
operations are rejected. The worker selects a source plan before mutating a
target; missing, ambiguous, or mismatched source identity blocks or rejects the
request with a bounded reason.

The canonical checksum uses the normalized v2 projection encoded as
NFC-normalized, sorted-key, compact UTF-8 JSON. Each step checksum covers its
normalized structured step, and the release checksum covers the product
version together with app identity, provenance, desired schema, and every
transition plan. A release's app identity must match the registered app. The
v2 migration removes the v1 release shape; it does not
translate historical v1 rows or provide an alias/fallback path, so operators
must resolve any legacy registry state before applying the release.

Legacy adoption no longer accepts an operator-supplied expected fingerprint.
It derives the expected value from the v2 desired-schema projection and only
adopts an exact canonical baseline. The same projection/fingerprint helper is
used by inventory drift, rollout postflight, and adoption preflight.

## SDK facade

`control-plane.gen.ts` is the generated operation/type boundary. The facade in
`control-plane.ts` centralizes URL normalization, path encoding, JSON encoding,
Bearer selection, idempotency headers, and the existing `akbFetch` error
runtime. It exports no data-plane constructor, raw request helper, or SQL/table
surface. The packed consumer exercises every control-plane operation and checks
the public subpath and declaration files.

## Immutable-source resume

`POST .../rollouts/{rollout_id}/resume` locks the source attempt and creates a
new job and sealed snapshot. The source job, snapshot, targets, steps, audit,
and checkpoints are never updated. The new attempt reuses the source release
and checksum, requires a fresh UUID idempotency key, and performs a live
preflight of the source target set: active grant generation, observed release
and generation, ownership, and the validated manifest. Targets already on the
release are persisted as `replayed` with no steps; remaining targets receive
the existing canary-one and sequential-ten batch policy. A source/key replay
returns the same new attempt, while a non-blocked source or input mismatch is
rejected before mutation.

## Generic runtime proof

The repository-owned `app-control-plane` descriptor uses random, source-neutral
fixture identities and declares schema-v2 readiness, discovery, health, reset,
and bounded control hooks. It reuses the existing fixture-control lifecycle;
it does not start or warm a runtime during unit proof. Independent behavior
execution remains a deferred acceptance gate.
