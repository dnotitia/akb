# @akb/client — Changelog

The reference SDK in `packages/akb-client`. Its version is the `version` in
`packages/akb-client/package.json`. Entries start with 0.2.0; earlier changes
are in the repository history.

## 0.2.0 — Change Event channel reconnects honour Retry-After

The Change Event channel no longer tries a refused open again every 250 ms.
The event tail answers 503 `event_tail_capacity` and `event_tail_unavailable`
with `Retry-After`, and the server still checks a refused caller's access
within its tail database budget, so fixed fast retries could slow the reads
of streams already open.

- An open refused with a 5xx, one that fails before a response, and one whose
  stream ends before its first event or comment, as when a proxy accepts the
  request and drops it, are tried again after a backoff. With the default
  reconnection time it is a random time between 250 and 500 ms after the
  first in a row, a range that doubles with each further one; a longer SSE
  `retry` raises where it starts. The backoff never waits more than 30
  seconds. A stream's first event or comment, a heartbeat included, resets
  it; a bare 2xx response does not.
- A refusal's `Retry-After`, as seconds or an HTTP-date, is waited in full,
  however long, or the backoff or the reconnection time when that is longer,
  so a `Retry-After` of zero does not skip the backoff. When the `Retry-After`
  is positive, a random extra of less than 20% of it, and less than one second,
  goes on top of the longer wait, so that clients refused together come back
  apart; a zero `Retry-After` adds none.
- An HTTP-date is measured from the response's `Date` header when it has a
  valid one, which also gives an RFC 850 two-digit year its century. A date
  whose day name is not its own is invalid, and a leap second is the second
  after :59.
- A stream that ends after sending an event or a comment is still reopened
  after the reconnection time: 250 ms, unless the stream's SSE `retry` field
  set another.
- An event that a stream ends in the middle of, before the blank line that
  ends it, is discarded, as the SSE parsing rules require, and the stream is
  reopened from the last cursor. It used to be dispatched, or to end the
  channel with `protocol_error` when its JSON was cut.
- A CRLF line ending split between two chunks of a stream is one line ending.
  The LF used to read as an empty line, which dispatched the event early and
  ended the channel with `protocol_error`.
- `AkbError` has a new `retryAfterMs` field: the wait the response's
  `Retry-After` asked for, in milliseconds, or `null`. HTTP error responses
  set it, from `akbFetch` as from the channel, and so do the SDK-local
  `invalid_json` and `response_read_error` for a response whose body could
  not be read; they keep nothing else of that response but its status.
- A 4xx refusal still ends the channel. 429 `event_tail_user_quota` rejects
  `subscribe()`, or `closed` once a stream has run, with an `AkbError` that
  carries its `code`, `status` and `retryAfterMs`.
