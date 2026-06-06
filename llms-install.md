# Installing the AKB MCP server

This guide is written for an AI coding agent (e.g. Cline) configuring the
`akb-mcp` server on a user's behalf. Humans: the same values work in any
MCP client.

`akb-mcp` is a small **stdio proxy** (zero dependencies) that bridges your
agent to an **AKB backend**, which speaks MCP over Streamable HTTP. So you
need two values:

- **`AKB_MCP_URL`** — the AKB backend's MCP endpoint, e.g.
  `https://akb.example.com/mcp/`
- **`AKB_PAT`** — an AKB Personal Access Token (looks like `akb_...`)

## Where to get the two values

- **Public demo (zero setup — best for trying it):**
  - `AKB_MCP_URL` = `https://akb-demo.agent.seahorse.dnotitia.ai/mcp/`
  - `AKB_PAT` = sign up at <https://akb-demo.agent.seahorse.dnotitia.ai> with
    any email (a throwaway is fine) and create a token.
  - ⚠️ Public, weekly-wiped demo — don't store anything real or sensitive.
- **Self-hosted:** run AKB (`docker compose up -d`, see the README quick
  start), then create a PAT in the web UI or via
  `POST /api/v1/auth/tokens`. Your `AKB_MCP_URL` is `https://<your-host>/mcp/`.

## MCP server configuration

Add the server to your MCP client config. For Cline this is
`cline_mcp_settings.json`:

```json
{
  "mcpServers": {
    "akb": {
      "command": "npx",
      "args": ["-y", "akb-mcp"],
      "env": {
        "AKB_MCP_URL": "<paste the AKB_MCP_URL from above>",
        "AKB_PAT": "<paste the AKB_PAT from above>"
      }
    }
  }
}
```

`npx -y akb-mcp` fetches the proxy from npm — no build step. The proxy also
accepts `--url <URL> --pat <TOKEN>` as CLI args instead of env vars, and
`--insecure` to skip TLS verification against a self-signed backend.

## Verify it works

After the server connects, have the agent call:

- `akb_whoami` → should return the authenticated user.
- `akb_search` with any query → hybrid dense + BM25 search over the vaults.

Troubleshooting: a `401` means `AKB_PAT` is wrong or expired; a connection
error means `AKB_MCP_URL` is unreachable (check the scheme, host, and the
trailing `/mcp/`).
