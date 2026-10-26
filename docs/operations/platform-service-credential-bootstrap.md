# Fixed-purpose service credential bootstrap

The trusted Native CLI provisions one installation-owned `platform-bot` service
account and its service credential. It requires direct authority over that
installation's PostgreSQL database. It adds no HTTP mint permission: management
OAuth credentials still cannot use the generic human token-issuer routes.
Both explicit Bare Git and PostgreSQL Native revision configurations use the
same authentication contract.

Use the installed backend's `app.yaml` and `secret.yaml`, with explicit SSO mode,
HTTPS public issuer, a configured service-authority client, positive runtime
generation and session epoch. The PostgreSQL authentication runtime must match.
Do not use this command to adopt a pre-existing account or recover a lost token.

Prepare a private JSON file (0400 or 0600, root or process owner) with exactly:

```json
{
  "schema_version": 1,
  "purpose": "platform-service-v1",
  "operation_id": "11111111-1111-4111-8111-111111111111",
  "org_id": "22222222-2222-4222-8222-222222222222",
  "tenant_slug": "example",
  "namespace": "akb-example",
  "owner_uid": "33333333-3333-4333-8333-333333333333",
  "token_id": "44444444-4444-4444-8444-444444444444",
  "credential": "<akb_secret_ followed by the unpadded base64url encoding of 32 random bytes>"
}
```

Generate the credential with a cryptographically secure random source. Persist
this exact intent before running the command; retries must reuse every field.
Kubernetes atomic Secret projections are accepted only when their resolved
regular file remains beneath the specified mount directory. Namespace and slug
must agree. No user, role, scope or key-class choices are accepted.

```sh
python -m app.cli provision-platform-service-credential \
  --intent-file /protected/intent.json --output-file /protected/receipt.json
```

The fixed account is `platform-bot`, email `platform-bot@<tenant_slug>.local`,
active pure service administrator. Its token has class `service`, scopes
`read`, `write`, `admin`, and no vault restriction. The command writes only a
secret-free receipt, never a credential to stdout or stderr. The output may be
a pre-existing regular termination-log file; its JSON is bounded to 4096 bytes.
No service-account token or Kubernetes API permission is needed by this CLI.

The receipt includes the intent identity, actual user/token UUIDs, fixed
account profile, initial runtime tuple, `status` (`issued` or `replayed`), and
SHA-256 of the raw credential and canonical intent JSON. Canonical JSON uses
sorted keys, ASCII escaping, compact separators and no trailing newline.
A receipt proves committed issuance, not successful HTTP authentication: verify
the credential through `/api/v1/auth/me` before publishing it to consumers.

Account, token hash, issuance lineage and the canonical PostgreSQL user-role
projection commit in one role-authority transaction on the same connection.
First issuance creates the NOLOGIN `akb_user_<user UUID with underscores>` role
and its normal `akb_authenticated` membership. Projection or audit failure
rolls back the account, token, lineage, role, membership and event together.
The unscoped service credential does not receive a scoped `akb_token_*` role.
Replay verifies the existing canonical membership and its options without
recreating a missing role or restoring a revoked or changed grant. The permanent
lineage has no cascading user/token foreign keys
and is independent of audit retention. Identical retries only confirm the
same unchanged live credential. A different intent, deleted/revoked/expired
token, suspended/demoted or changed account, changed scope/class/vault authority,
or changed initial authentication runtime is refused without repair. Deleting
an account or losing the intent file does not reopen bootstrap. Recover such
states through an explicitly reviewed administrative procedure.

For an actual image-owned semantic check, after successful CLI issuance:

```sh
python -m app.services.platform_service_bootstrap_probe \
  --intent-file /protected/intent.json --receipt-file /protected/receipt.json \
  --output-file /protected/probe.json
```

This requires the real committed lineage, replays the exact intent, resolves the
actual credential through the normal token-store authentication path, and
checks that service OAuth remains ineligible for generic issuance. It does not
seed a token or substitute a fixture principal. The advertised capability is
`auth.platform-service-bootstrap.v1`; image provenance and an actual deployment
Job plus HTTP verification remain separate acceptance evidence.
