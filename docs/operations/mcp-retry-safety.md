# MCP retry safety

A lost response does not prove that a write failed. The backend may have
committed before the connection disappeared, and a client timeout does not
cancel or roll back that work. The stdio proxy therefore separates reconnecting
the backend session from repeating the interrupted tool call.

## Automatic replay policy

Only explicitly reviewed reads retry connection failures, at most three
dispatch attempts including the original call. Successful reads can observe a
newer state on a retry; this is not a snapshot guarantee. The current allowlist
is in `packages/akb-mcp-client/lib/proxy.mjs` (`REPLAY_SAFE_TOOLS`).

- `akb_grep` is a read when `replace` is omitted or null. Every other supplied
  value is ineligible for automatic replay. In particular, `replace: ""`
  performs replacement and is a write.
- All `akb_sql` calls are ineligible, including queries beginning with SELECT
  or WITH. The proxy does not infer effects by parsing SQL prefixes.
- Writes and unclassified tools are dispatched at most once per proxy call.
  Adding a tool or advertising `readOnlyHint` does not opt it into replay.
- Background session recovery, tool-cache refresh and list-change notifications
  continue. Protocol `ping` and `tools/list` are also safe to repeat.

The policy applies to `_forward()`. Proxy-local file/image tools use separate
REST/S3 paths and do not gain new retries or this outcome envelope. Existing
input validation and original-read revision/hash predicates are unchanged.

## Failed calls and outcome evidence

Transport failures of forwarded tools are tool error results (`isError: true`)
with a JSON text body. Both supported protocol generations carry the same
meaning. These generated errors contain no arguments or raw upstream bodies.

| Code | `details.operation_outcome` | Evidence |
| --- | --- | --- |
| `backend_unavailable` | `not_started` | Preflight failed before entering tool RPC dispatch |
| `operation_unknown` | `unknown` | Dispatch was entered, but a reliable result was not received |

```json
{
  "error": "The operation outcome is unknown. Check the current state before repeating the request.",
  "code": "operation_unknown",
  "details": { "operation_outcome": "unknown" }
}
```

Dispatch evidence is conservative and local to each invocation. Even a socket
error immediately after entering RPC dispatch is `unknown`; the proxy does not
infer non-execution from an error string or a socket-write callback. A failed
preflight on a later read retry cannot erase an earlier dispatch. Exhausted read
retries use this same envelope.

An interrupted/invalid response or a non-protocol HTTP 5xx response is also
unknown after dispatch. Explicit MCP protocol errors and HTTP rejections retain
their existing error path; normal tool errors such as `conflict`, access denial,
or `vault_skill_required` are forwarded without automatic replay. A legacy
session-loss response restores the connection but does not replay a write.

## Caller recovery

For `not_started`, a caller can try again after backend connectivity recovers.
For `unknown`, retain the intended change and inspect current state first.
For a document, compare the current content and metadata with the draft. For
SQL or creation, use the application's resource identity and checks; do not
assume that the absence of a success response means nothing was committed.

Keep the original read predicates. Fetching a newer revision solely to retry
an old-derived draft defeats concurrency protection. A second write reporting
`conflict` would not prove that the first one failed, either.

The proxy does not generate idempotency keys or expose an operation-status API.
Native document callers can supply a key using the backend's
[document idempotency contract](document-idempotency.md); this does not opt
writes into automatic proxy replay.
State inspection is not a durable receipt for the original request. Other
clients, explicit caller retries and other infrastructure can still duplicate
operations; this policy is not an exactly-once guarantee.

## Verification

`packages/akb-mcp-client/test/replay-safety.test.mjs` uses real loopback HTTP
connections and stdio processes. Its fixture records a change before dropping
the response and asserts one effect, while read calls retain retry behavior.
It also covers empty replacements, SQL, unknown tool hints, partial responses,
timeouts, malformed responses, known errors, legacy session loss and concurrent
dispatch evidence. These fault fixtures do not substitute for backend storage
or transaction tests. Run the package's `npm test` and `bash scripts/check.sh`.
