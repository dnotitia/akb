# Private profile photos

People signed in with a human local or SSO browser session can upload,
replace and remove their own photo in **Settings → Profile**. Choose an image,
review its centered-square preview, then select **Save photo**. Photo changes
are separate from the display-name and email form. Removal returns the account
to initials after an inline confirmation.

The saved photo appears in Profile, the account-menu trigger and the account
menu. It is visible only to its owner: this feature does not expose photos to
Vault members, document authors, search results or public links. It neither
imports OIDC `picture` URLs nor updates an identity provider. SSO-managed name
and email fields remain read-only.

## Storage and upgrade

Photos reuse the configured S3-compatible store: `s3_endpoint_url`, `s3_bucket`,
`s3_auth_mode` and the existing authentication settings. There is no new required
setting, separate bucket or browser-side storage credential. Without configured
object storage, the account keeps initials and explains why photo editing is
unavailable. A configured but unreachable store produces a recoverable request
error rather than a claim that the feature is unsupported.

Upgrade the backend and apply startup migration **114** before updating the
frontend. API and worker processes must use matching code, database schema and
storage settings: the updated deletion worker recognizes live account-photo
references as well as Vault files. Do not mix old deletion workers with new
photo writers. New frontend code connected to an older backend keeps initials
and displays an unsupported-feature notice; frontend-only deployment cannot
enable uploads.

The database holds current photo metadata in `user_avatars` and the write budget
in `user_avatar_write_budget`. Objects use server-generated keys under
`.akb-account-assets/avatars/<user UUID>/<revision UUID>.webp`. Ensure the existing
bucket policy permits API/worker reads, writes and deletes under that prefix.
Browsers read through the authenticated API, never directly from the bucket.
Storage keys, bucket names and credentials are not returned as photo metadata.

Photos are account assets, not Vault files: they do not create document Git
commits, search chunks, public publications or Vault inventory entries. Back up
the PostgreSQL references and corresponding storage bytes together. Restoring
only one store does not restore a working photo. Downgrading to an old worker
without photo-reference checks is not a safe rollback procedure.

## Image limits

Inputs must be still JPEG, PNG or WebP images, at most **10 MiB**, **12,000,000
pixels** and **8,192 pixels per side**. The server verifies decoded content and
MIME agreement, rejects animations and corrupt images, corrects EXIF orientation,
then center-crops and re-encodes to **512×512 WebP**, at most **256 KiB**. Original
bytes and EXIF/GPS/XMP/ICC metadata are not retained. Interactive crop adjustment,
GIF, SVG, HEIC and AVIF are not supported.

The body admission limit is two requests per API process, with a five-second
admission wait and a 30-second body deadline. Document attachment inspection and
photo normalization share two decoder slots per process. Photo decoding has a
five-second admission wait and a 20-second cancellation deadline. Cancellation
does not forcibly terminate a native decoder thread; its slot remains held until
the thread actually settles. Each account has a ten-attempt, one-minute write
budget; exhausted requests return `429` with `Retry-After: 60`.

## API and authentication

All paths below are relative to `/api/v1`.

| Endpoint | Contract |
| --- | --- |
| `GET /auth/me` | Adds `avatar` and `profile_capabilities.avatar`; does not fetch image bytes or ping object storage. |
| `PUT /auth/me/avatar?expected_revision=...` | Raw image body with its actual Content-Type; returns the confirmed avatar state. |
| `GET /auth/me/avatar?v=<revision>` | Returns only the current owner's current photo, with `image/webp`, `private, no-store` and `nosniff`. |
| `DELETE /auth/me/avatar?expected_revision=...` | Returns a new revision and `url: null`; already-absent removal with the current revision is idempotent. |

Only the owner's active human local JWT or SSO browser session is accepted.
SSO writes use the existing cookie and CSRF boundary. PAT, OAuth bearer,
service-account and delegated requests cannot use the photo endpoints; there is
no photo SDK/MCP surface or administrator endpoint for another person's photo.
Session validity is checked again after a slow transfer, so a revoked session
cannot publish or return delayed photo bytes.

Writes require the revision returned by the server; use `none` only before the
first photo. A photo-less state starts as `{"revision":null,"url":null}`. Removal
retains a new tombstone revision even though its URL is null. This prevents an
old tab from treating a removed photo as a new account state and overwriting it.
A stale write returns `409 avatar_changed` without replacing current state.

## Recovery and cleanup

An unsuccessful upload retains both the saved photo and selected draft. The user
can retry, choose another image or cancel without losing text-profile edits.
Invalid file size and invalid resolution have separate messages. A revision
conflict requires **Reload photo** and a fresh selection; dismissing an error or
a failed reload does not unlock a stale save. If the server commits but its
response is lost, retrying detects the revision conflict instead of writing a
second replacement. Reload retrieves the committed photo.

A durable delayed cleanup is queued before each remote PUT. Publication of the
new reference and cancellation of that candidate cleanup share one database
transaction; old-object deletion is queued in the same transaction. Failed
transfers, database rollback and cancelled requests leave candidates available
for cleanup. Account self-deletion and administrative deletion both enqueue the
current photo before removing its owner. The worker checks current ownership
and the shared object-key lock before deleting bytes.

The existing S3 deletion worker retries failed deletes. Operators can inspect
queue state without retrieving user photos or storage keys:

```sql
SELECT count(*) FILTER (WHERE processed_at IS NULL) AS pending,
       count(*) FILTER (WHERE processed_at IS NULL AND abandoned_at IS NOT NULL) AS abandoned,
       min(next_attempt_at) FILTER (WHERE processed_at IS NULL) AS next_attempt_at
FROM s3_delete_outbox
WHERE s3_key LIKE '.akb-account-assets/avatars/%';
```

Delayed candidate cleanup counts as pending and is not, by itself, an outage.
Investigate abandoned work and storage access failures through existing worker
diagnostics. Never delete a live object's bytes solely because an outbox entry
mentions it.

## Regression coverage

`backend/tests/test_user_avatar.py` covers formats, metadata removal, resource
limits, authentication, CSRF, HTTP and OpenAPI contracts.
`backend/tests/test_user_avatar_postgres.py` uses disposable PostgreSQL databases
for revision races, delayed transfers, cancellation, session revocation and
cleanup. It is also selected by the live-PostgreSQL CI job. Its opt-in
`AKB_TEST_AVATAR_S3_ENDPOINT` case exercises an isolated S3-compatible store.
Never point those fixtures at a shared application database.

Frontend photo API/cache tests and Settings tests cover initials, account
changes, unsaved edits and recovery. `frontend/e2e/profile-photo.spec.ts` covers
responsive light/dark flows and the SSO browser contract.
`profile-photo-live.spec.ts` exercises actual storage and local reauthentication
only when explicitly enabled with `AKB_PROFILE_PHOTO_LIVE=1` in real Playwright
mode against an owned isolated local runtime. It creates and removes its own
accounts. Browser contract fixtures do not substitute for an external Keycloak
login acceptance check in the deployment's own identity configuration.
