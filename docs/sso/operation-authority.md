# Provider ownership and bounded identity operations

An installation can explicitly assign a provider alias to an external
controller. Unowned providers retain the standalone administrator and service
provider-management API. Assigning ownership is a human administrator ceremony,
not a side effect of a controller discovering an alias. No controller name,
private alias or external platform URL is built into this contract.

The active-realm paths retain standalone administration. Installed management
only targets additionally allow preparation of another realm while the source
verifier stays unchanged, including a local source. A management target never
becomes a second verifier, a public login choice or an OIDC credential issuer.

## Discover and adopt

All paths below are relative to `/api/v1`. The existing administrator service
PAT can read `/admin/sso/operation-capabilities` and
`/admin/sso/providers/{alias}/ownership`. It cannot grant or retire operations or
assign ownership. Human calls use the product-admin session; mutations also
require the matching `X-AKB-Admin-CSRF` cookie/header proof.

Capabilities return `schema_version: 1`, the database-owned `installation_id`,
`max_ttl_seconds: 600`, `max_nonce_age_seconds: 300` and these behavior names:

- `sso.provider-ownership.v1`
- `sso.identity-operation.v1`
- `sso.enabled-exact-admission.v1`
- `sso.preparation-target.v1`
- `auth.callback-login-evidence.v1`
- `sso.operation-attempts.v1`

`realm_identity` is `{realm_id, realm_name, issuer}` from an actual Admin REST
read of the configured realm. Missing permission, delegated management and
unavailable or invalid realms return `realm_identity: null` with a value-less
`realm_identity_error`. A controller must treat that as unknown. An issuer URL
or a bootstrap marker does not establish that the realm UUID survived a restore.
The normal management client needs `view-realm`, already part of the bundled
management role set. No arbitrary realm URL is accepted in the request.

Ownership readback contains `{schema_version, target_id, provider_alias, owner_id,
revision, fingerprint, drifted, emergency_disabled, provider_secret_revision}`. For an absent alias,
`owner_id` is null and `revision` is initially zero. A human adopts or reserves
an alias with:

```json
{
  "owner_id": "controller-installation-id",
  "expected_revision": 0,
  "expected_fingerprint": "<fingerprint from live ownership GET>",
  "clear_emergency": false
}
```

Send this to `PUT /admin/sso/providers/{alias}/ownership`. Every successful
change advances the revision. `owner_id: null` explicitly releases ownership.
Fingerprint and revision mismatches fail rather than adopting a stale read.
Alias spelling must already be canonical: whitespace or other normalization
changes are rejected before ownership lookup, locking or provider control.
An invalid provider configuration remains inspectable and fingerprintable.
A human can release its ownership with the live CAS values and repair it through
the ordinary configuration API; new grants and execution remain blocked until
the provider is valid.

Ordinary configure and enable requests for an owned alias are forbidden. A
human administrator may still disable it through the existing API. This records
`emergency_disabled`, advances the revision and fences earlier grants. Clearing
the emergency intent requires another explicit human CAS ownership request with
`clear_emergency: true`; a controller cannot clear it.

## Issue an operation

`POST /admin/sso/identity-operations` requires the human session and CSRF proof:

```json
{
  "operation_id": "<fresh UUID>",
  "installation_id": "<capabilities UUID>",
  "actor_id": "<existing service-user UUID>",
  "actor_token_id": "<existing service PAT UUID>",
  "provider_alias": "workforce",
  "owner_id": "controller-installation-id",
  "expected_provider_revision": 1,
  "runtime_generation": 1,
  "sso_session_epoch": "<current UUID>",
  "ttl_seconds": 600,
  "verbs": ["preflight", "apply", "rollback"],
  "cohort": [{
    "existing_user_id": "<existing AKB UUID>",
    "upstream_subject": "<exact opaque upstream subject>",
    "broker_subject": "<exact prelinked broker subject>"
  }],
  "provider_payload_sha256": {}
}
```

The response includes `schema_version`, `operation_id`, `expires_at`,
`cohort_sha256`, `provider_revision` and a one-time `credential` with no-store
headers. Only its hash is stored. Securely transfer the credential to the
intended controller; never place it in ordinary desired-state metadata or logs.
The operation is bound to the installation, actor/token, owner, provider
revision/fingerprint, runtime generation/epoch, verbs and at most 25 exact
cohort items. Both issuers are derived and frozen server-side. Grants fail for
local accounts, missing old bindings, different issuers and bindings belonging
to another AKB account. No email-based adoption or account merging occurs.

For provider-only operations, `cohort` is empty and verbs are selected from
`configure`, `enable`, `disable`. `provider_payload_sha256` must contain exactly
one digest per granted provider verb. A configure payload uses the existing
provider configuration request fields; toggle payloads are exactly `{}`.
The digest is SHA-256 of UTF-8 JSON with lexicographically sorted ASCII field
keys, no whitespace and preserved Unicode (`ensure_ascii=False`). Payloads
contain strings/null, so numeric canonicalization is not involved. Secrets are
not included in a grant, operation row, result or audit event, only in the
execution request to the existing provider security profile.

## Execute, retry and retire

Send `POST /admin/sso/identity-operations/{operation_id}/execute` with
`Authorization: AKB-Operation <credential>`:

```json
{
  "nonce": "<fresh UUID>",
  "issued_at": "2026-10-21T00:00:00Z",
  "verb": "apply",
  "item_index": 0,
  "payload": null
}
```

Identity verbs use a cohort index. Provider verbs require index zero and the
exact payload whose digest the human approved. Ordinary PATs and browser
cookies cannot execute this route. This dedicated header carrier does not use
browser CSRF. A nonce is valid for at most 300 seconds and never beyond the
operation's expiry. The same nonce and request digest return the stored result;
changing the digest is a conflict. Retirement, revoked/expired actor tokens,
granting-admin demotion, runtime changes, ownership changes and drift reject
both new writes and credential-based replays.
Expiry uses the current database wall clock after lock waits and immediately
before effects; waiting behind another operation does not extend authority.

`apply` and `rollback` require the provider disabled. `admit` is a separate,
explicitly granted verb for an enabled provider and the same proven exact
upstream cohort. It never disables a provider to admit one person. Preflight
does not mutate. An operation rolls back only a binding it actually inserted,
identified by that row's UUID. Existing AKB UUIDs, PATs, Vault ownership and ACLs
are preserved. AKB mutation, nonce receipt, ownership of the inserted binding
and audit event commit atomically; a changed Keycloak postcheck rolls them back.

Provider execution advances both the ownership revision and the operation's
effective revision. Its original grant revision remains in the journal. A raw
Keycloak success followed by a process/database failure can leave observable
drift without a committed receipt. This is an explicit reconciliation state:
do not blindly repeat the write or re-enable the provider. A human must inspect
and adopt the live fingerprint before issuing another operation.

Human administrators inspect a secret-free operation with
`GET /admin/sso/identity-operations/{id}` and retire it with
`POST /admin/sso/identity-operations/{id}/retire`. Those paths also accept the
bounded operation carrier while valid. Retirement retains the journal. No
automatic Keycloak user/link deletion is performed.

## Installed management targets

Set `sso_management_targets_file` to an installation-owned secret-store file.
The default is empty and exposes no preparation target. The JSON document is:

```json
{
  "schema_version": 1,
  "targets": [{
    "target_id": "<stable installation-owned UUID>",
    "realm_id": "<actual Keycloak realm UUID>",
    "realm_name": "workspace",
    "issuer": "https://login.example/realms/workspace",
    "internal_url": "http://private-keycloak:8080",
    "management_client_id": "akb-sso-manager",
    "management_client_secret": "<secret-store value>",
    "session_epoch": "<future installed runtime epoch UUID>",
    "verify_ssl": true
  }]
}
```

The regular file must be owned by root or the process UID and mode `0400` or
`0600`. Its contents are never returned by HTTP. The issuer must be canonical
for the installed realm; callers cannot register URLs or override credentials.
After switching, `sso_prepared_target_id` selects the installed target UUID for
the separate prepared-bootstrap receipt contract; its default remains empty.
The narrow management client must already exist. Preparing it is an independent
host-side Keycloak ceremony; this API does not grant realm administration.

`GET /admin/sso/targets` returns the installation ID, active target ID, exact
source tuple and installed target metadata. Every realm identity is read from
that target's actual Admin REST endpoint. A failed read is unknown, not a healthy
realm. `GET /admin/sso/targets/{target_id}/providers` returns the existing schema1
provider catalog envelope with `target_id`; its `auth_mode: sso` describes the
management target, while inventory `source` describes the active verifier.
Target-specific ownership GET/PUT append `/providers/{alias}/ownership`.
Ownership is keyed by target UUID and canonical alias, so identical aliases in
source and preparation realms do not collide. Durable issuer/UUID pins reject
remapping or fallback after a target file is removed, including local sources. After a verifier switch, active
legacy paths resolve the same installed target UUID, independently of epoch.

Target grants add `target_id`, `expected_target_realm_id`,
`target_session_epoch`, `expected_target_runtime_generation`,
`expected_target_source_revision` (full source SHA or null), and `expected_source`:
`{auth_mode, issuer, runtime_generation, sso_session_epoch, source_revision}`.
Local source issuer/epoch are null. Every request pins the protected target
configuration and actual realm UUID. Removed, changed or recreated targets fail
closed; no source-control or broader-credential fallback exists.

All provider and identity mutations require the exact original source tuple
still live in the database and process. After transition, only status, challenge,
event read and retirement accept the explicit future target tuple, whose
generation must advance. No caller boolean can assert that transition occurred.
Identity prelink still requires the exact existing upstream binding; installing
management credentials does not convert local or different-issuer accounts.

Optional grant `provider_secret_revision` becomes known ownership metadata only
when the approved configure request supplies an actual nonnull `client_secret`.
Omitting that secret preserves an already verified version; visible drift makes
readback return null. Version metadata alone does not prove masked Keycloak
secret contents. Keep the approved request digest and host secret-store receipt.

Operation status includes `target_id`, `source_tuple`, `target_epoch`, current
`provider_revision`, `cohort_sha256`, `phase`, and nullable
`existing_identity_ids_hash` / `prepared_identity_ids_hash`. Phases are
`granted`, `preparing`, `prepared`, `retired`. Hashes are SHA256 of canonical JSON
sorted AKB UUID arrays, populated only after actual whole-cohort inspection;
prepared is set only when every exact binding is linked. Explicit `postcheck`
uses the same safe inspection contract as preflight.

## Actual callback login evidence

Grant `login-evidence` for the exact identity cohort. The bounded carrier can
POST `/admin/sso/identity-operations/{id}/login-challenges` with
`{"akb_user_id":"<cohort UUID>"}`. The response contains operation/challenge/user
IDs, expiry and a no-store `login_url` with an opaque challenge. Only its hash is
stored. A browser can start it only under the exact granted target runtime.
Optional `request_id` is a durable per-user attempt UUID. Reusing it returns the
same challenge and original expiry; another user conflicts. The opaque value is
derived from the authenticated operation credential, so retry needs no plaintext
credential storage. The server carries the challenge ID in its one-use nonce/PKCE
OIDC state.

The actual Keycloak callback verifies both token profiles, resolves the exact
existing AKB identity, and creates the opaque browser session. In that same
transaction it consumes the challenge and records the event. Invalid state,
unverified claims, revoked bindings, expired authority or stale runtime cannot
create evidence. A query parameter or a platform callback is not login proof.
There is no event writer API.

GET `/admin/sso/identity-operations/{id}/login-events` with exact
`akb_user_id` and `challenge_id` accepts the bounded carrier or the exact
original actor service PAT. Another token or a user outside the cohort is denied.
It returns `events` bound to operation/challenge, installation/target, canonical
AKB user, verified issuer/provider/subject, runtime generation/epoch/source SHA,
cohort hash, occurrence time and nullable provider/secret revision. Empty results
are not success. A consumer must require a fresh event matching its transition
journal, exact target tuple and known provider/secret revision (or an independent
fresh journal supplement); null means unknown and cannot establish finality.

GET `/auth/identity-capabilities` is public secret-free corroboration with
schema1, supported behavior names and the actual active source tuple. Source
revision is the baked full40-character SHA or null; a product version is never
substituted. This endpoint cannot attest to its own image provenance or Pod
digest, which remain deployment-owned evidence.

## Fresh authority after an interrupted attempt

Authority never refreshes automatically. An expired nonce or credential requires
another explicit human approval. Read the root operation's `intent_sha256`, then
POST `/admin/sso/identity-operations/{root_operation_id}/authority-grants` using
the current human product-admin ceremony (CSRF for cookie-backed SSO):

```json
{
  "operation_id": "<fresh child authority UUID>",
  "expected_intent_sha256": "<root immutable intent hash>",
  "ttl_seconds": 600
}
```

No actor, cohort, payload, target or source override is accepted. The backend
derives those values from the original root grant, verifies the exact current
runtime and fresh ownership/readback, and checks the live actor token and new
granting human. Under the original SOURCE it permits the original approved verbs
(`authority_scope: source-recovery`). Under the explicitly bound future TARGET it
permits only `login-evidence`, readback and retirement (`target-evidence`). The
original source tuple is retained, never rewritten to describe the target.

A successful response contains the one-time credential and the root
`parent_operation_id`, unchanged `intent_sha256` and monotonic
`authority_attempt`. Issuance atomically retires previous credentials. Concurrent
human approvals leave only the newest attempt live. The controller must CAS its
canonical credential receipt by parent/intent/attempt; a late earlier response
must not replace a newer credential. Lost credential responses require another
human attempt, because plaintext credentials are never stored. Every attempt
has its own nonce namespace; an old nonce is never given a fresh timestamp.

Status can be read by the exact original actor service PAT even after expiry or
supersession. This metadata read is not execution or renewal authority. New
children cannot be issued after explicit retirement closes the lineage. Visible
drift, ownership/revision changes, emergency disable, changed targets or a revoked
actor token block reauthorization and require separate human reconciliation.

Status additionally reports live `owned_binding_ids` (external-identity row
UUIDs), `owned_binding_count`, committed `rollback_results` and fresh nullable
provider state/fingerprint/secret revision or a safe `provider_readback_error`.
A SOURCE child can compensate only exact row UUIDs inserted anywhere in its
immutable lineage. A subsequently recreated row is not owned and is preserved.
Rollback does not restore independently revoked PATs, changed ACLs or other
administrative decisions. These fields do not prove comprehensive abort: no
Keycloak user/link deletion, provider cleanup or broad PAT/ACL restoration is
claimed. A controller must block an unproven abort or use an independently
verified host compensation ceremony; it cannot turn caller flags into evidence.

## Trust boundary and artifact evidence

Full Keycloak realm administrators can bypass these API guards. The guarantee
is API ownership enforcement plus detection of visible raw configuration drift,
not an immutable realm policy. Masked secrets prevent a readback fingerprint
from detecting every out-of-band secret replacement. Preserve emergency intent
and require independent login/secret-revision evidence for transition finality.

The existing machine account-governance API is still trusted to project roles,
activate and offboard already admitted AKB IDs. Approved machine token issuers
retain their explicitly configured issuance authority. The bounded operation
credential does not isolate the installation from complete compromise of those
broader trusted authorities. Machine account projection cannot add a new
binding to an existing AKB ID outside the bounded operation; an exact existing
binding can still be refreshed. Fresh admissions under the exact configured
issuer remain distinct from migration. Human standalone administration remains.

Machine administrator SQL now uses its ordinary PostgreSQL user/token role;
`is_admin` alone does not delegate the backend database role. Granted Vault SQL
continues through RoleSync. Live human system administrators retain their
existing full SQL trust boundary.

Version labels alone do not establish support. Genuine `backend-v0.14.2`
(`b28b345e687ae35441abec545961c9d09b39ea18`) predates the provider-admin route.
Snapshot `9c705f0a03360c9ac788341dfc56f367bdda9b06` is 147 commits later.
Candidate admission must combine immutable artifact/source provenance with
behavior fixtures. Source-native PostgreSQL/HTTP tests establish this contract's
AKB behavior; their substituted Keycloak readbacks are not proof of live
Keycloak permissions or a particular deployed image.
