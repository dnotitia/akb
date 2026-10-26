# Prepare an existing Keycloak identity target

These explicit commands support a controlled identity transition. Preparation
runs on a trusted host with a protected input file and **does not import AKB
settings or connect to the AKB database**. The running human verifier, source
recovery principal, PATs, identity bindings, and ACLs remain unchanged.

Use the same backend source/image for preparation and adoption. The Keycloak
realm must already exist: its name, actual UUID, public issuer, and internal URL
are pinned in the input. A missing/recreated realm is refused, never replaced.
The existing standalone default and receipt loader keep their previous behavior.

## Host input and output

Create a private directory (0700) and JSON input (0600). The input is the typed
`HostPreparationInput` in `app/sso/prepare_target_host.py`. It includes:

- `schema_version: 1`, canonical `operation_id`, `org_id`, `target_id`, actual
  `realm_id`, `realm_name`, public `issuer`, `internal_url`, future
  `session_epoch`, `akb_public_url`, and exact `backchannel_logout_uri`.
- Platform-generated independent API, admin, and six-role management client IDs
  and secrets; the independently generated browser-session encryption key.
- One-time master bootstrap client ID/secret, a native recovery password and
  email. Its username is always `akb-recovery-<target UUID without hyphens>`.
  `product_admin_subject` pins an existing native user UUID, or is `null` to
  allocate a new operation-owned user and read its actual UUID back.
- Up to 25 cohort members, each with an existing AKB user UUID, exact upstream
  issuer/subject, provider alias and broker username. `broker_subject` pins an
  existing broker user UUID; `null` explicitly requests a new operation-owned
  allocation. Keycloak 26 generates UUIDs even when a create body supplies `id`.
  Only actual read-back UUIDs appear in the resolved receipt cohort.
- Optional `provider`: the approved `keycloak-oidc` platform broker spec, exact
  callback, upstream broker secret version, and non-null secret. Only alias
  `platform` is accepted. The existing provider adapter imports and validates
  discovery and creates the provider disabled. An existing alias must carry this
  exact operation/target bootstrap marker; a foreign/customer alias is refused.
  Otherwise the target provider must already exist with the approved issuer.
- `source_guard`: original `auth_mode`, nullable issuer/session epoch, positive
  `runtime_generation`, and a 40-character `source_revision`. This is carried as
  a binding; the platform remains responsible for fresh source-state readback.

Do not put these values in command arguments, shell history, application YAML,
Kubernetes Jobs, or a Kubernetes Secret containing bootstrap authority.

The one-time master client must issue access tokens with `exp - iat` at most
600 seconds. Preparation and cleanup reject malformed, expired, future-issued,
non-Bearer tokens or a master issuer different from the pinned public Keycloak
root before target mutation. This check inspects only tokens acquired from the
configured token endpoint; Keycloak authenticates their signatures and rights.
It does not change the default standalone bootstrap token policy.

```sh
python -m app.sso.prepare_target_host \
  --input-file /private/transition/host-input.json \
  --output-dir /private/transition/prepared
```

The command reuses the standalone Keycloak profile adapter. It configures exact
callbacks, signing/rotation policy, AMR, broker password guard, realm events and
login presentation. The permanent manager retains exactly the established six
roles, without `manage-clients`, `manage-users`, or master access. Existing realm
administrator rights and the full Keycloak administration UI remain available.
One admin-only user-profile attribute records operation ownership; arbitrary
unmanaged attributes remain disabled.

The cohort is checked for credentials, required actions, foreign federation and
duplicate ownership before seeding. There is no email lookup, merge, or AKB DB
projection in this phase. A private host journal and exact ownership readback
recover uncertain creates/links and retirement responses; a retry never rotates
a client secret or blindly recreates an uncertain user. Do not remove that
journal while a run is incomplete. An interrupted process releases its advisory
lock; the next run can resume. Each actual allocated UUID is checkpointed before
linking. A restart can resolve an uncertain allocation only by exact username
plus the original operation marker and durably pins that UUID before further
work; a later same-name user cannot replace it. `--compensate-cohort` is available
while preparation authority still exists and deletes only unchanged operation-owned cohort users.

Successful output files are 0600:

- `sso-management-targets.json`: schema 1 installed descriptor containing only
  the permanent manager secret and exact target/realm/issuer/epoch binding.
- `active-credentials.json`: the exact pre-generated API/admin/manager secrets
  and browser-session key for the later explicit switch.
- `target-preparation.json`: secret-free profile/client UUID readback, resolved
  cohort, provider secret revision/hash, original source guard, immutable user
  fingerprints, file hashes, and historical retirement attestation.

Before publishing the receipt the command deletes the one-time client and proves
both old credential token acquisition and the previously issued master token's
admin call are denied. The host journal containing that token is then removed.
Keep raw authority/input files on the trusted host and dispose of them through
the operator's secret lifecycle. Only the three successful outputs belong in
the canonical target Secret. Mount the receipt/descriptor as regular 0400 files
(for Kubernetes, `subPath`), because readers reject symlink leaves.

## Adopt after the verifier switch

Install the descriptor while keeping source human settings unchanged. The
platform later explicitly activates target human settings and the exact active
credentials, sets `sso_prepared_target_id` to the installed target UUID, then runs:

```sh
python -m app.sso.adopt_prepared_target \
  --input-file /etc/akb/identity-target/target-preparation.json
```

Adoption refuses an unswitched runtime, missing selector, changed descriptor,
secret/key mismatch, wrong actual realm/client UUID, or invalid profile. It uses
fresh permanent-manager readback of the target realm, issuer, clients and current
security profile. Signing-key rotation is checked by current policy; the original
key ID is historical evidence. **Master retirement is a protected historical
host attestation**, not a fresh master observation: the manager has no authority
to inspect the master realm and is never granted any.

Only after these gates does adoption initialize/migrate storage and call the
canonical exact SSO recovery-admin provisioner with an explicit target UUID.
Migration 124 preserves the global standalone receipt and original recovery user
and creates a distinct target-scoped recovery principal. Username or email
collisions are refused rather than adopted. Source user IDs/bindings, PATs and
ACLs are not updated. The immutable target receipt is selected only by explicit
target UUID plus matching realm/issuer; there is no latest-row or source fallback.
An interrupted projection/receipt write converges on the same target principal.

## Compensate a prepared target before transition abort

Use a **new**, short-lived master service client whose secret stays on the host.
Its protected `CleanupAuthority` input pins operation/target/realm and a distinct
client ID. Supply the original host input and completed preparation receipt:

```sh
python -m app.sso.cleanup_prepared_target \
  --input-file /private/transition/host-input.json \
  --preparation-file /private/transition/prepared/target-preparation.json \
  --authority-file /private/transition/cleanup-authority.json \
  --output-file /private/transition/prepared/cleanup-compensation.json
```

Cleanup first compares every operation-created user's actual UUID, operation
marker, selected user state, credentials, links and role mappings with the host
snapshot, then rechecks each exact fingerprint immediately before its DELETE.
A detected change refuses that deletion; final readback reports actual remaining
objects. Keycloak user GET/DELETE is not atomic: a realm administrator or another
writer can still change state between the last read and deletion. Perform cleanup
in a controlled maintenance window; the command provides no server-side lock
against full realm administration. Pre-existing/adopted users,
clients, providers, realm administration and unrelated resources are preserved.
After exact owned-user deletion it reads back absence and retained existing IDs,
retires the cleanup client, and proves old-token/new-credential denial before
writing the secret-free compensation receipt. A failed preflight retains the
private host journal for an explicit repair/retry, without claiming retirement.
The platform must independently verify source guard, AKB operation rollback and
preserved AKB IDs before accepting Abort.

## Focused verification

Run the unit contract suite from `backend/`:

```sh
python -m pytest tests/test_prepared_sso_target_unit.py
```

The opt-in `test_prepared_sso_target_live.py` fixture requires an explicit private
Docker SSH host through `AKB_PREPARED_TARGET_DOCKER_HOST`, and
`AKB_PREPARED_TARGET_REMOTE=1`. It allocates uniquely named resources, a private
PostgreSQL 16 instance and pinned Keycloak 26.7.0, tests real HTTP/DB behavior,
and removes only its resources. It uses ports 55476 and 55890; run it exclusively
for that owned fixture allocation. It does not build or deploy product images.
