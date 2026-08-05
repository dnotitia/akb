---
status: proposal
stage: design
created: 2026-08-05
updated: 2026-08-05
---

# MCP stdio proxy survives backend/VPN outages without a session restart

## What this is

The `akb-mcp` stdio proxy (`packages/akb-mcp-client`) bridges a client's
stdio MCP transport to the AKB backend's Streamable HTTP `/mcp/` endpoint.
When the VPN (or backend) is down for a long stretch, the AKB server
silently disappears from the client's tool namespace for the rest of the
session — `mcp__akb__*` tools stay gone even after connectivity returns,
until the client/session is restarted.

This item decouples the **client-visible liveness** of the proxy from
**backend reachability**, so a transient or long outage degrades and
recovers instead of permanently de-registering the server.

## Root cause

The proxy answered the MCP `initialize` handshake by round-tripping to the
backend (`_rpc("initialize", …)`). A stdio MCP server that returns an error
to `initialize` is treated by the client as *failed to initialize* and is
dropped for the whole session. So:

1. VPN down at proxy/session startup → backend `initialize` fails.
2. Client marks the AKB server failed, never registers its tools.
3. VPN returns; `claude mcp list` shows the server healthy, but the running
   session's tool namespace was fixed at startup — tools do not come back
   without a restart.

The proxy **process** was alive the entire time. The defect was the
coupling: making client-visible liveness depend on a backend round-trip.

Naive "infinite retry" on tool calls does **not** fix this (the server is
already de-registered) and introduces a worse footgun — a tool call that
hangs forever on a blackholed connection.

## Design

Client-visible liveness is served locally; the backend session is managed
out of band.

1. **Local `initialize`.** The proxy answers the handshake itself — echoes
   the client's `protocolVersion` (fallback `2025-06-18`), advertises
   `capabilities.tools.listChanged = true`, reports `serverInfo`. The server
   always registers, backend up or down. (Consistent with prior behavior,
   the client's `notifications/initialized` is not forwarded; the backend
   accepts `tools/*` after a bare `initialize` over this transport.)

2. **Backend session, single-flight.** `_ensureBackend()` lazily performs
   the backend `initialize` (capturing `mcp-session-id`) under a
   single-flight lock, returning a boolean — never throwing. Used by both
   the monitor and `_forward`.

3. **Resilient `tools/list`.** Serves a live or cached backend tool list.
   With the backend unreachable and no cache, it degrades to the local file
   tools only (a valid partial response) and sets `_servedDegraded`. The
   backend list is cached on first success; decoration (file-param
   injection + appended file tools) never mutates the cache.

4. **Background reconnect monitor.** Exponential backoff (1s → 30s cap),
   effectively forever while the client is connected (`_closed` on stdin
   EOF stops it). On (re)connect it refreshes the tool cache and, if the
   client had been served a degraded list, pushes
   `notifications/tools/list_changed` — the full toolset reappears with no
   session restart. Idempotent: at most one loop runs.

5. **Non-fatal tool calls.** A connection error in `_forward` marks the
   backend not-ready, kicks the monitor, and retries; a persistent outage
   surfaces a normal JSON-RPC error for that single call rather than tearing
   anything down.

6. **Connect-phase timeout.** `AKB_MCP_CONNECT_TIMEOUT_MS` (default 10s)
   arms only while a socket is still `connecting`, so a blackholed
   connection is detected quickly — separate from the generous 5-min
   response timeout retained for legitimately slow ops (`akb_delete_vault`
   on large vaults). Liveness probes (`initialize`/`tools/list` from the
   monitor) use this short timeout so the loop cycles fast.

## Why not the alternatives

- **Literal infinite retry per tool call** — hangs the agent on a dead
  connection and still leaves the server de-registered. Rejected.
- **Backend-driven `initialize` with more retries** — still couples
  liveness to reachability; more retries only widen the startup stall.
- **A separate local sidecar/proxy server** — more moving parts and a new
  failure surface for what is a client-liveness decoupling problem.

## Known limitation

If the backend goes silent mid-session on an already **established**
keep-alive socket (no RST), the *first* in-flight tool call can wait up to
the response timeout before the retry/monitor kick in. Subsequent calls
fail fast because the monitor has flipped the ready flag. A first-byte
timeout shorter than 5 min would close this, at the cost of risking
false aborts on legitimately slow operations — deferred deliberately.

## Scope / non-goals

- No change to tool behavior, auth (PAT bearer), or the S3 file-transfer
  path.
- Backend `/mcp/` (`http_app.py`) is unchanged. This is a pure client-side
  proxy resilience change.
- OAuth/`--transport http` remote registration is a separate connection
  path and out of scope.

## Verification

- `packages/akb-mcp-client/test/reconnect.test.mjs` (new): local
  `initialize`, degraded `tools/list`, cache decoration without mutation,
  monitor `list_changed` on recovery (and silence when never degraded),
  `_forward` retry/recover/surface, process survival.
- `packages/akb-mcp-client/test/contract.test.mjs`: unchanged, still green.
- `npm test` in the package runs both.

## Release

- Proxy is versioned independently: **2.0.4 → 2.1.0** (`package.json`,
  `server.json`, `CHANGELOG.md`).
- Proxy changes require an **npm publish**, which is a deliberate human
  gate — not performed by the agent. After publish: git tag + GitHub
  Release per the repo's proxy release flow.
