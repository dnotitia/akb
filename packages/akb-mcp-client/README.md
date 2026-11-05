# akb-mcp

`akb-mcp` connects a local MCP host to an AKB installation and handles local file transfers. It supports browser OAuth and Personal Access Tokens. Both modes use one selected credential for MCP and the admitted file and image controls.

## Connect with browser OAuth

Use an OAuth-enabled installation's MCP endpoint:

```sh
npx akb-mcp --url https://akb.example.com/prefix/mcp/
```

An MCP definition needs only the endpoint:

```json
{
  "mcpServers": {
    "akb": {
      "command": "npx",
      "args": ["-y", "akb-mcp", "--url", "https://akb.example.com/prefix/mcp/"]
    }
  }
}
```

The proxy opens the system browser after its first local protocol response is flushed. Sign in in that browser, then submit a new tool call on the same MCP connection. Initialization, discovery, the complete initial tool list, ping, and `akb_connection` remain available while authorization is pending.

The installation must advertise its protected resource, one authorization server, and `akb_native_client_id`. Its registered public native client must support Authorization Code, S256 PKCE, refresh rotation, and `http://127.0.0.1:<dynamic-port>/oauth/callback`. No client secret or prior CLI login is required. The proxy requests the advertised AKB read and write scopes and does not request `offline_access`.

The transport endpoint is `B/mcp/`, including any installation prefix. The OAuth resource and token audience are exactly `B/mcp`. Both input spellings select that direct transport endpoint. Control and discovery redirects are refused.

## Connect with a PAT

`--pat` and `AKB_PAT` deliberately select PAT mode. For example:

```sh
AKB_PAT='<your-pat>' npx akb-mcp --url https://akb.example.com/mcp/ --auth-mode pat
```

PAT-only installations need no OAuth metadata, authorization server, browser, SQLite store, or refresh flow. A rejected PAT asks you to replace the selected token and restart. It never starts browser login. A URL-only invocation selects OAuth only when no PAT input is present.

`--auth-mode oauth` conflicts with a supplied PAT. Contradictory options, unknown options, duplicate flags, and missing values fail before network or store access.

## Check and recover the connection

Call `akb_connection` with an optional `action`:

| Action | Behavior |
| --- | --- |
| `status` | Default. Returns selected `mode`, `authentication`, and `connectivity`. Backend and OAuth discovery connectivity are also reported separately. |
| `sign_in` | Starts one bounded OAuth attempt. Repeated calls do not start a second live attempt. |
| `cancel` | Cancels this process's OAuth attempt and closes its callback listener. It does not cancel another process's owner. |
| `sign_out` | Clears the shared local OAuth session. If an attempt owns the session, cancel or finish it and retry sign-out. |

OAuth actions in PAT mode return `inapplicable_action`. They do not change the selected method.

Authorization lasts at most three minutes per attempt. If automatic browser opening fails, status exposes a copyable `authorizationUrl` for that same attempt. Denial, timeout, cancellation, sign-out, rejected credentials, or an interrupted token exchange require an explicit recovery action. A discovery outage reports unavailable connectivity and retries discovery without rejecting a saved grant or opening another browser.

Calls made before readiness return `execution: "not_started"` and are discarded. Completing sign-in never executes an earlier call. Caller operations are submitted once. A reconnect, token refresh, 401, cancellation, or lost response never resubmits them.

Multi-step file failures include a receipt with `stage`, `vault`, and any known `file_id`, `replacement_id`, or `uri`. Earlier progress produces `execution: "partial"`. A lost dispatched response produces `execution: "unknown"`. Inspect the retained reservation or resource before deciding on a new call.

## Local files and authority

The initial catalog contains every generated server tool plus six local file and image tools and `akb_connection`. `akb_put` and `akb_update` also accept a local `file` argument. Installation-specific features can remain unavailable even though their grammar is listed. An incompatible live schema requires the matching proxy version.

File and image controls obtain the current credential for each request. Read and write scopes remain independent, and existing vault and document permissions still apply. The selected tool credential has no authority on unrelated account, browser, administrator, or service REST routes. Existing administrator MCP permissions remain unchanged.

Byte transfers use individual capabilities without the general bearer. Redirects are refused. Downloads use stream backpressure and verify exact size and SHA-256 before replacing a local destination. File reservation, confirm, replacement, OCC, MIME, and exact-operation vault-guide acknowledgement rules remain active. A write-only grant can pass the specific insufficient-read guide response without acquiring read scope. Other authentication and ACL errors propagate.

## Runtime and private storage

Use a supported Node.js release, `^22.23.4 || ^24.22.1 || ^26.12.1`. OAuth storage supports macOS and Linux local filesystems. OAuth is not supported on Windows or network filesystems. PAT mode does not import or open OAuth SQLite storage.

OAuth uses `~/.local/state/akb-mcp` by default. Set `AKB_MCP_AUTH_DIR` to a dedicated private local directory to override it. Directories require owner-only permissions, and files require private ownership with no symlinks or hard links. Persisted OAuth credentials are permission-protected plaintext, not encrypted storage.

The grant binds the exact resource, issuer, and client ID. Processes sharing that directory and installation coordinate browser and refresh ownership through SQLite with a zero busy timeout. A suspended owner remains busy. Process death releases ownership without a timed takeover. A durable intent records a token exchange before dispatch, and a nonsecret publication marker admits the replacement pair only after the credential record is durable. An interrupted or uncertain exchange requires fresh authorization and never retries the old refresh token.

Local sign-out affects other proxy processes using that same shared store. Different installations have separate records. Closing stdin cancels this process's attempt and active requests and closes the loopback listener.

All endpoints require HTTPS. HTTP is accepted only for loopback development. The configured MCP URL must not contain user information, a query, or a fragment. There is no insecure TLS flag.

## Build and verify the package

Maintainer packing requires the full repository checkout, `uv`, the locked backend Python environment, and the package's locked Node dependencies:

```sh
uv sync --locked --project backend
npm --prefix packages/akb-mcp-client ci
npm --prefix packages/akb-mcp-client test
npm pack ./packages/akb-mcp-client
```

`prepack` always checks schema structure, tool-name collisions, the pinned production SDK, protocol version consistency, and canonical generator drift. A missing backend generator or failed check stops packing. Regenerate the artifact after a deliberate server grammar change:

```sh
uv run --locked --project backend python scripts/generate_mcp_catalog.py \
  --output packages/akb-mcp-client/lib/generated-tools.json
```

Registry tarball installs and `npx` consumption do not run `prepack`. Consumers need Node and declared production dependencies, including the pinned MCP client SDK. They do not need Python, `uv`, the source backend, or Inspector.

Package tests exercise production storage and actual stdio processes with a mocked authorization server. They also install an npm tarball with production dependencies and use the installed CLI through the real SDK. These tests do not establish real IdP or native-host OAuth acceptance.

The repository's Inspector smoke compares every live HTTP tool schema with the shared stdio grammar, records exact file-argument and acknowledgement composition, checks all local tool schemas, and identifies installation-filtered initial-only tools. Unexpected schema differences fail the comparison. It also retains the representative `akb_list_vaults` result comparison.
