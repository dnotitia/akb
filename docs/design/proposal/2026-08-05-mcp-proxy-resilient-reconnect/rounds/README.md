# Rounds

- **Framing correction.** The request arrived as "make the proxy infinite-retry
  so it recovers after a long VPN drop." Reading the code showed infinite retry
  is the wrong lever: the proxy process never dies on connection failure — a
  failed backend `initialize` makes the *client* drop the server for the whole
  session. The fix therefore targets the handshake coupling, not the retry
  count. Literal per-call infinite retry was rejected for adding a hang-forever
  footgun without addressing de-registration.

- **Recovery mechanism.** A background reconnect that only re-establishes the
  session is not enough when the client was handed a degraded (file-tools-only)
  list at startup — the client would keep showing the partial toolset. Settled
  on advertising `tools.listChanged` at the local `initialize` and pushing
  `notifications/tools/list_changed` on recovery, so the full toolset returns
  without a session restart.

- **Timeout split.** Kept the deliberate 5-min response timeout (added in 2.0.2
  for large `akb_delete_vault` cascades) and added a separate short
  connect-phase timeout for blackhole detection, rather than shortening the one
  timeout and risking false aborts on slow ops. The mid-session dead-keepalive
  edge was acknowledged as a known limitation rather than papered over.

- **Backend-handshake consistency.** Verified the pre-existing proxy already
  dropped the client's `notifications/initialized` (never forwarded it), so a
  locally-answered `initialize` plus a bare backend `initialize` matches the
  previously-working request sequence — no new dependency on an `initialized`
  notification.
