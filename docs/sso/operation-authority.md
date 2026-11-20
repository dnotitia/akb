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
assign ownership itself. A separate dedicated service key can ask a human
through an [operation request](#operation-requests). Human calls use the product-admin
session; mutations also require the matching `X-AKB-Admin-CSRF` cookie/header
proof.

Capabilities return `schema_version: 1`, the database-owned `installation_id`,
`max_ttl_seconds: 600`, `max_nonce_age_seconds: 300` and these behavior names:

- `sso.provider-ownership.v1`
- `sso.identity-operation.v1`
- `sso.enabled-exact-admission.v1`
- `sso.preparation-target.v1`
- `auth.callback-login-evidence.v1`
- `sso.operation-attempts.v1`
- `sso.provider-client-credentials.v1`
- `sso.provider-initialize.v1`
- `sso.operation-requests.v1`
- `sso.operation-request-service-key.v1`

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

Send this to `PUT /admin/sso/providers/{alias}/ownership`, or let a controller
propose it as an `ownership_claim` request. Every successful change advances
the revision. `owner_id: null` explicitly releases ownership.
Fingerprint and revision mismatches fail rather than adopting a stale read.
Alias spelling must already be canonical: whitespace or other normalization
changes are rejected before ownership lookup, locking or provider control.
An invalid provider configuration remains inspectable and fingerprintable.
A human can release its ownership with the live CAS values and repair it through
the ordinary configuration API; new grants and execution remain blocked until
the provider is valid.

Ordinary configure, enable and client-credential requests for an owned alias
are forbidden. A human administrator may still disable it through the existing
API. This records `emergency_disabled`, advances the revision and fences earlier
grants. Clearing the emergency intent requires another explicit human CAS
ownership request with `clear_emergency: true`; a controller cannot clear it.

`provider_secret_revision` is an opaque label for the configured client secret.
A client-credential change of an unowned provider
(`PUT /admin/sso/providers/{alias}/client-credentials`, see the SSO README)
records it together with the new fingerprint and revision, creating an unowned
row if none exists; that request is refused while the emergency latch is set.
Enabling and disabling, a human emergency disable included, keep the label,
because neither changes the secret, so after the latch is cleared the ownership
read still names the configured secret version. An ordinary configure and a
release of ownership clear it.

## Initialize an absent provider

When a provider alias is absent, an authorized provider-control actor may use
`PUT /admin/sso/providers/{alias}/initialize`. Supply the ordinary
configuration fields `provider_type`, `display_name`, `issuer`,
`discovery_url`, `client_id` and `client_secret`, plus the
`expected_fingerprint` from the absent-provider ownership read and a
nonempty `secret_revision` label. The label is an opaque revision of the
secret in the caller's secret store, never the secret itself.

Initialization requires an unowned alias without an emergency latch and creates
the provider disabled. It does not enable the provider. The operation checks
the absent fingerprint under the ownership lock and checks Keycloak again after
discovery; creation uses only Keycloak's create operation. A concurrent
creation causes a conflict and is never overwritten. The normal provider
response is secret-free; ownership readback shows the resulting fingerprint,
revision and secret-revision label.

Keycloak and the AKB database are separate authorities. If a response is lost
or the database or audit write fails after Keycloak accepted creation, the
provider can exist without the corresponding AKB evidence. Do not automatically
retry initialization after an ambiguous result: inspect the provider and
ownership readback, then reconcile explicitly. Existing providers require the
ordinary configuration or client-credential paths.

The `sso.provider-initialize.v1` capability advertises this endpoint.
The independent `sso.operation-request-service-key.v1` capability advertises
Native issuance of the dedicated request key; the older
`sso.operation-requests.v1` capability alone does not establish that support.

## Operation requests

A direct grant (next section) returns the raw operation credential to the
administrator's browser, so an automation could only obtain a human's approval
if the human copied a credential to it. An operation request removes that step:
the automation proposes, a human approves the exact request in `/admin`, and
only the token that asked collects the credential, once, sealed to a key it
generated for that request. No response returned to a human contains a
credential.

`POST /admin/sso/operation-requests` accepts only a service key with exactly
the `sso_operation_requests` scope and no vault scope, belonging to an active
non-admin service account. Provision it using the
[Native bootstrap](#provision-the-requester-key). Broad read, write and admin
keys do not acquire this authority. The requesting user and token are taken
from the calling token; the body cannot name them. Two kinds exist:

```json
{
  "kind": "ownership_claim",
  "provider_alias": "workforce",
  "owner_id": "controller-installation-id",
  "expected_revision": 0,
  "expected_fingerprint": "<fingerprint from live ownership GET>"
}
```

```json
{
  "kind": "provider_grant",
  "operation_id": "<fresh UUID>",
  "provider_alias": "workforce",
  "owner_id": "controller-installation-id",
  "expected_provider_revision": 1,
  "verbs": ["configure", "enable"],
  "provider_payload_sha256": {"configure": "<digest>", "enable": "<digest of {}>"},
  "configure_summary": {
    "provider_type": "keycloak-oidc",
    "display_name": "Company SSO",
    "issuer": "https://id.example.com/realms/workforce",
    "discovery_url": "https://id.example.com/realms/workforce/.well-known/openid-configuration",
    "client_id": "akb-broker",
    "client_secret_supplied": true
  },
  "ttl_seconds": 600,
  "provider_secret_revision": "<optional opaque label for this secret version>",
  "recipient_public_key": "<raw 32-byte X25519 public key, unpadded base64url>"
}
```

A claim always names an owner; releasing ownership stays a human decision. A
grant carries only `configure`, `enable` and `disable`: no identity, cohort or
login-evidence verb can be requested. `configure_summary` is required exactly
when `configure` is requested and holds every configure field except the
secret, plus whether a secret is supplied. Payload digests follow the direct
grant's canonical JSON rule. An `operation_id` can be requested once.

The response (`201`) is the request view: `request_id`, `request_sha256`,
`request`, `status`, `created_at`, `expires_at`, `requester_username`,
`decided_by`, `decider_username`, `decided_at`, `operation_expires_at` and
`closed_at`. `request` is the complete immutable request document: the fields
above, the database `installation_id`, the active `target_id`, the requesting
`requested_by` and `requested_token_id`, and for a grant the active `source`
tuple. `request_sha256` is the SHA-256 of that document's canonical JSON, so
anyone can recompute it. A request nobody decides expires 3600 seconds after it
was proposed.

`GET /admin/sso/operation-requests` and `GET .../{request_id}` accept a human
product-admin session, which sees every request, or the requesting service
token, which sees only its own. Undecided requests are listed first, at most
100 in all.

`POST .../{request_id}/approve` and `POST .../{request_id}/deny` require a human
product-admin session and the CSRF header, and the body
`{"expected_request_sha256": "<digest the screen showed>"}`. A different digest,
or a request that is no longer pending, is a `409` conflict, so a stale screen
cannot decide anything it did not show. Approval also requires the requesting
token to still be the live dedicated service key.

- Approving a claim runs the ownership compare-and-set above with the approving
  human as the actor and `updated_by`, in the transaction that records the
  approval. A revision or fingerprint that moved since the proposal is a `409`
  and the request stays pending.
- Approving a grant records the approval only and fixes
  `operation_expires_at = decided_at + ttl_seconds`.

`POST .../{request_id}/cancel` withdraws a pending request, or an approved grant
not yet collected; only the requesting token can call it.

`POST .../{request_id}/credential` collects an approved grant. Only the exact
requesting token can call it, only once (a second call is `409`
`sso_operation_request_already_collected`) and only before
`operation_expires_at`; the response carries `Cache-Control: no-store`. At that
moment every check of a direct grant runs again against the current state: the
exact source tuple, the same target, owner, provider revision and fingerprint,
no emergency latch, the installation, the live requesting token, and that the
approving human is still an active administrator. Then AKB creates the
operation row and its credential, exactly as a direct grant does, with
`granted_by` set to the approving human and `expires_at` set to
`operation_expires_at`, so the operation never outlives the human approval by
more than its TTL. Only the credential hash is stored, and the request row keeps
only the public key. The response:

```json
{
  "schema_version": 1,
  "request_id": "<UUID>",
  "request_sha256": "<digest>",
  "operation_id": "<UUID>",
  "expires_at": "<operation expiry>",
  "provider_revision": 1,
  "sealed_credential": "<unpadded base64url>",
  "seal": {
    "mode": "base",
    "suite": "DHKEM(X25519, HKDF-SHA256)/HKDF-SHA256/ChaCha20-Poly1305",
    "info": "akb.sso.operation-request.credential.v1:<request_id>:<request_sha256>"
  }
}
```

The seal is HPKE (RFC 9180) base mode, single shot, with KEM `0x0020`, KDF
`0x0001` and AEAD `0x0003`. `sealed_credential` is the 32-byte encapsulated key
followed by the ciphertext, and `info` binds it to this request. Open it with
the private key generated for this request, then use the credential exactly as
a direct grant's credential. A stolen service key alone can at most collect a
sealed credential it cannot open; the rightful requester then receives the
`409` and can raise an alarm. A lost collection response requires a new
request.

For a `configure` verb from a request, execution additionally requires every
payload field other than the secret, and whether a secret is supplied, to equal
the approved `configure_summary` (`403 sso_operation_summary_conflict`). Direct
grants carry no summary and are unchanged.

Events, each without a credential, a sealed value or a secret:
`auth.sso_operation_request_proposed`, `_approved`, `_denied`, `_cancelled`,
`_expired` (once per request, recorded by the next request API call after its
window passed) and `_credential_issued`.

`/admin` shows, per provider, the owner, revision, emergency latch, drift and
whether a secret revision is recorded; lists pending requests with the
requesting service account, kind, alias, owner, verbs, summary, TTL and "secret
held by the requester, not shown"; and binds approve and deny to the digest it
displayed. It also releases ownership and clears the emergency latch, each a
confirmed compare-and-set on the revision and fingerprint shown; the release
confirmation states that releasing clears the recorded secret revision. An
owned provider shows "Managed by an external owner" instead of the configure and
enable controls; disable stays available.

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
bounded operation carrier while valid. The dedicated requester key can inspect
and retire only operations originating from its own collected requests and
their human-reauthorized attempts, bound to the same user and token.
Retirement retains the journal. No automatic Keycloak user/link deletion is performed.

## Provision the requester key

This command is a trusted Native operator action with database authority. It is
the only issuance path for this scope; generic HTTP token minting cannot issue
it. Before running it, create and durably retain a fresh credential in the
operator's secret store. It must be `akb_secret_` followed by the unpadded
base64url encoding of 32 cryptographically random bytes. Choose a fresh UUID
for the token ID and retain both values across retries.

Pipe the retained credential directly to standard input, and write the
secret-free receipt to a protected path:

```sh
cat /protected/requester-key | python -m app.cli provision-approval-service-credential \
  --token-id "$REQUESTER_TOKEN_ID" --receipt-file /protected/requester-receipt.json
```

Standard input must be a pipe; the value never appears in command arguments,
stdout, stderr, the receipt or audit events. The receipt includes
`requester_user_id`, `requester_token_id`, `installation_id`, `key_class`,
`scopes`, `vault_scope` and `status` (`issued` or `replayed`). Store the
identity metadata with the secret. Retrying the same UUID and value returns
the same identity; a changed value, a deleted key, or an altered account is
refused. The ledger retains a tombstone even when an account or token is deleted.

For rotation, retain a new credential under a new token UUID and run the same
command. It adds a key to the same non-admin requester without revoking the
prior key. Existing requests and operations stay bound to their original user
and token; revoke an old key only when its outstanding work no longer needs it.

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
