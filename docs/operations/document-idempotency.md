# Retrying Native document writes

`akb_put`, `akb_update`, REST `POST /documents` and REST document `PATCH`
accept an optional `idempotency_key` UUID in their input body. PostgreSQL
Native supports it; Bare Git rejects a supplied key with HTTP 501 / code
`document_idempotency_unsupported`. Calls without a key retain existing
behavior. An explicit null also means no key.

MCP discovery advertises `_meta["akb/document_idempotency"].supported` on
the two document tools. The field remains in the accepted schema on Bare Git
so an explicit unsupported request gets a stable error rather than silently
running without duplicate protection. Deploy the updated backend before
using keys; older REST deployments may ignore unknown fields.

## Caller behavior

Generate one UUID for one intended write, and keep the complete request until
the result is known. After a timeout or `operation_unknown`, repeat that exact
request with the same key. Do not generate a fresh key, replace the original
revision/hash condition, or change the body. A new intended change needs a new
key. A successful retry returns the original write receipt, including its
revision, path and content hashes, even if the document has changed since.
It does not return the latest document state; use `akb_get` for that.

```json
{
  "vault": "example",
  "collection": "notes",
  "title": "Draft",
  "content": "Original body",
  "idempotency_key": "11111111-2222-4333-8444-555555555555"
}
```

Keys are scoped to the authenticated user's immutable UUID and the Vault's
identity. Tokens for the same user share that scope; another user or another
Vault has an independent scope. Once a request commits, reuse within that scope with a different
operation, target, content, metadata, or original-read condition returns
`conflict`. Null and omitted update fields normalize to their existing no-op
semantics; empty strings/lists remain explicit values. Defaults and Unicode
normalization follow the request models. List order is significant.

Every HTTP/MCP attempt checks current write access, token scope and reserved
Vault-guide permissions before looking up a receipt. A key grants no access.
Native lookups and writes use the Vault UUID recorded by the access check.
If the name resolves to a replacement Vault, the request fails with `conflict`
before it can read that Vault's receipt or write a revision.
Update routes still resolve the target for those checks; deletion or an
unresolvable target can return `not_found` rather than an old receipt. The
receipt is minimal write metadata and never contains the document body.

The stdio proxy forwards the key unchanged, including when it resolves `file`
to `content`. Keep the file bytes unchanged for a retry. The proxy's safe-retry
policy still does not automatically replay writes, even those with keys.
The TypeScript client's `docs.put`/`docs.update` use the camel-case
`idempotencyKey` option and forward it as `idempotency_key` without generating
or replacing it.

## Persistence and concurrency

The server derives a mutation UUID from the actor, Vault and public key, and
hashes the operation plus normalized request. It stores these in the existing
Native revision's `mutation_id` and `request_fingerprint` fields. No new
request table, body copy, migration or process-local receipt cache is needed.

A lookup precedes timestamp generation, automatic path disambiguation and
title/OCC conflicts. Publication verifies the same identity under the existing
transactional mutation lock. Concurrent matching requests produce one revision,
activity entry and invalidation intent; different inputs have one winner and
a conflict. A commit occurring between the optimistic lookup and a facade
conflict causes a receipt recheck. Replays reconstruct immutable receipt fields
from the original revision and parent, rather than using a losing attempt's
generated timestamps or merged body. A failure before commit creates no receipt:
validation errors and rolled-back attempts do not reserve a key.

Receipt identity has **no time-based expiration**: it is retained with the
existing immutable revision history. Document soft deletion keeps that history;
Vault deletion cascades it. There is no additional unbounded request-body store.
Do not reuse a key for another intent, or assume replay across deleted/recreated
Vault identities or restored older backups. Future history-retention changes
must preserve this contract or explicitly version its lifetime.

This contract covers Native document create/update only. It adds no public
operation-status lookup, generic SQL idempotency, Bare Git guarantee, or
exactly-once delivery across unrelated clients and infrastructure.

## Verification

`test_document_idempotency_postgres.py` exercises actual PostgreSQL transactions,
simultaneous matching/conflicting calls, post-commit response loss, a new service
instance, stale original-read tokens, actor/Vault isolation, cross-surface
receipts, revoked membership and token scope. Unit tests cover malformed keys,
discovery and Bare Git rejection. The proxy contract tests cover local-file key
forwarding; existing write-precondition and metadata suites remain applicable.
`mcp_e2e/test_document_idempotency_e2e.py` also verifies that the common Bare Git
HTTP runtime advertises no support and rejects keys through authenticated
MCP/REST without mutating documents, while keyless calls remain usable.
