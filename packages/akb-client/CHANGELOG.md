# @akb/client — Changelog

The reference SDK in `packages/akb-client`. Its version is the `version` in
`packages/akb-client/package.json`. Entries start with 0.2.0; earlier changes
are in the repository history.

## 0.2.0 — Change Event channel reconnects honour Retry-After

The Change Event channel no longer tries a refused open again every 250 ms.
The event tail answers 503 `event_tail_capacity` and `event_tail_unavailable`
with `Retry-After`, and the server still checks a refused caller's access
within its tail database budget, so fixed fast retries slowed the reads of
streams already open.

- An open refused with a 5xx, one that fails before a response, and one whose
  stream ends before its first event or comment, as when a proxy accepts the
  request and drops it, are tried again after a backoff: a random time between
  250 and 500 ms after the first in a row, a range that doubles with each
  further one. A stream's first event or comment, a heartbeat included,
  resets it; a bare 2xx response does not.
- A refusal's `Retry-After`, as seconds or an HTTP-date, is the shortest wait
  it allows. An HTTP-date is measured from the response's `Date` header when
  it has a valid one. A `Retry-After` of zero does not skip the backoff.
- No wait exceeds 30 seconds, even for a longer `Retry-After`.
- A stream that ends after sending an event or a comment is still reopened
  after the reconnection time: 250 ms, unless the stream's SSE `retry` field
  set another.
- `AkbError` has a new `retryAfterMs` field: the wait the response's
  `Retry-After` asked for, in milliseconds, or `null`. HTTP error responses
  set it, from `akbFetch` as from the channel.
- A 4xx refusal still ends the channel. 429 `event_tail_user_quota` rejects
  `subscribe()`, or `closed` once a stream has run, with an `AkbError` that
  carries its `code`, `status` and `retryAfterMs`.
