# File object ownership and temporary cleanup

This describes the normal S3-compatible File path. Native measurement storage
has a separate lifecycle and is not covered by replacement-staging inventory.

## Confirmation and deletion

Ordinary upload confirmation changes a File from `pending` to `confirmed`.
An already-confirmed File is not an abandoned reservation: confirmation retries
return its current metadata without storage I/O or mutation. A supplied hash
must match its certified hash; a different or uncertified hash returns 409
without deleting anything. Legacy confirmed Files without certified hashes
remain readable; confirmation is not the operator integrity-repair mechanism.

Storage inspection runs outside a PostgreSQL transaction. Before either
publishing metadata or removing a failed reservation, confirmation locks and
rechecks the current File state and inspected object key. Events and the Native
projection intent are recorded only for an actual state transition, in that
same transaction. Browser upload errors do not imply that server confirmation
rolled back, so the browser never translates them into a general File DELETE.

Both direct File deletion and recursive collection deletion lock the current
File rows before selecting cleanup keys. The metadata deletion and S3 delete
outbox insertion commit together. Actual object-store deletion happens later,
outside the transaction, and rechecks global live references by physical key.

## Replacement staging inventory

Replacement uploads use this private namespace:

```text
__akb_file_replacements__/<vault>/<file UUID>/<replacement UUID>
```

Confirmation copies to a fresh candidate **outside** this namespace, verifies
the candidate, and atomically publishes its metadata while cancelling the
candidate's fallback deletion. The staging namespace is never a published File
location. Repeated confirmation uses a new candidate each time.

The issuance-time delayed delete remains the prompt cleanup path. It cannot
prove that an already-accepted PUT has finished: a slow PUT can recreate an
object after that delete. The S3 delete worker therefore also inventories the
actual staging namespace:

1. List at most 100 objects per page, at most once per 60 seconds per worker.
2. Consider only exact staging-key shapes at least 3,900 seconds old. This is
   the existing one-hour upload lifetime plus five-minute retention margin,
   not an increased upload timeout.
3. Exclude objects with an unexpired PUT intent or any live File reference.
   An expired intent may already have been reaped; missing metadata alone is
   not sufficient without the private namespace and age checks.
4. Enqueue eligible keys into the existing outbox, without duplicating an
   outstanding job. A failed DB lookup is an error, never proof of expiry.
5. Advance the page only after its DB work commits. At the end, start again
   from the beginning: a later write may recreate a key behind the cursor.

No DB connection is held while listing S3, and no transaction is held while
deleting an object. Inventory errors are logged as
`s3_delete_worker staging inventory failed` and retried on later passes. An
unavailable page is not skipped. A worker restart starts a fresh traversal.

The guarantee is **eventual collection**, assuming completed writes become
visible in listings, writers eventually stop, and inventory/deletion can run.
It is not synchronous prevention of late writes, an exact deletion deadline,
or an unlimited confirmation window for arbitrarily slow uploads. Multipart
object timestamps may reflect initiation, not completion; age must not be
interpreted as a post-completion grace period.

## Required storage permissions and operational boundaries

- In addition to existing File object operations, grant bucket listing
  restricted to `__akb_file_replacements__/` (on AWS, `s3:ListBucket` with
  the corresponding `s3:prefix` condition). Unrelated bucket prefixes are not
  inventoried. Verify this permission when updating an external bucket's IAM
  policy. The application does not rewrite bucket lifecycle configuration.
- Permanently denied listing/deletion is an operational error, not successful
  cleanup. Delete jobs keep the existing bounded retry/abandoned policy;
  terminal jobs require operator recovery and are included in the existing
  S3 outbox abandoned count. Inventory does not silently reset them.
- Current-object listing and key deletion do not reclaim all historical
  versions in a versioned bucket. Configure appropriate noncurrent-version
  and delete-marker retention if complete storage reclamation is required.
- Incomplete multipart uploads are not completed objects and are not in this
  inventory. The request handler attempts abort on failure; process loss still
  requires storage-side `AbortIncompleteMultipartUpload` lifecycle or an
  operator-managed multipart cleanup policy.
- This fix does not make ordinary initial-upload keys immutable against a PUT
  accepted while pending that finishes after confirmation. Replacement already
  publishes a distinct candidate; extending that publication protocol to initial
  uploads also requires preserving their hash-based deduplication contract.

References: [S3 object expiration](https://docs.aws.amazon.com/AmazonS3/latest/userguide/lifecycle-expire-general-considerations.html),
[multipart cleanup](https://docs.aws.amazon.com/AmazonS3/latest/userguide/mpu-abort-incomplete-mpu-lifecycle-config.html).
