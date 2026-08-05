# Feedback

- **PM origin.** Raised from an observed production symptom: after a long VPN
  outage the `akb` server vanished from a running Claude Code session's tool
  namespace (`mcp__akb__*` gone) while `claude mcp list` still reported the
  stdio proxy healthy. The diagnosis that startup-time `initialize` coupling —
  not retry count — was the cause came out of that discussion.

- **Verification run on the branch.**
  - `node test/reconnect.test.mjs` — 8/8: local `initialize` (backend down),
    protocol-version fallback, degraded `tools/list` → file tools only,
    cached full list served with `file` param injected and cache left
    unmutated, monitor emits `list_changed` on recovery from degraded, monitor
    stays silent when never degraded, `_forward` retries-then-surfaces a
    connection error (process survives), `_forward` recovers on a later attempt.
  - `node test/contract.test.mjs` — 6/6 unchanged (file-transfer destructure
    contract).
  - `node --check lib/proxy.mjs` — clean.

- **Gaps / not covered.** No full stdin→stdout integration test driving the
  real `start()` loop against a mock HTTP backend that flips down→up; the
  branch logic is covered at unit level by stubbing `_rpc`/`_ensureBackend`.
  The mid-session dead-keepalive first-call stall (see README limitation) is
  documented, not tested.

- **Release gate.** Proxy code changed → npm publish required, which is a
  human-only step. Not performed by the agent.
