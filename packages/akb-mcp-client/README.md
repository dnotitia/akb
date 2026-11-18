# Connect a stdio MCP host to AKB

Use the MCP URL supplied by your operator. Include its deployment prefix if
present, such as `https://akb.example.com/workspace/mcp`. Both modes require a
valid HTTPS certificate. HTTP is supported on loopback during development.

## Sign in with OAuth

Run the login command before you start your MCP host. Complete the sign-in
prompt in your browser.

```bash
npx akb-mcp login --url https://akb.example.com/mcp
```

Use the same URL in your MCP host. For Codex, add the following to
`~/.codex/config.toml`.

```toml
[mcp_servers.akb]
command = "npx"
args = ["akb-mcp", "--url", "https://akb.example.com/mcp"]
```

For Claude Code, run:

```bash
claude mcp add --transport stdio akb -- npx akb-mcp --url https://akb.example.com/mcp
```

A URL-only launch selects OAuth. The proxy starts with the saved grant and
refreshes it as needed. If the grant is missing or needs reauthorization, run
the login command separately. Submit a new tool call after you sign in.

Check the saved session or clear it with the following commands.

```bash
npx akb-mcp status --url https://akb.example.com/mcp
npx akb-mcp logout --url https://akb.example.com/mcp
```

Logout clears the grant for this URL in your local store. Other proxy processes
that share the store need a new login. The saved client registration remains
available for the next login.

The package declares its supported Node.js releases in `package.json`. OAuth
requires Node's built-in SQLite support and a local filesystem on macOS or
Linux. The proxy stores tokens as owner-private plaintext under
`~/.local/state/akb-mcp`. Set `AKB_MCP_AUTH_DIR` to an owner-private local
directory to isolate a test session. Start login and the MCP host as the same
OS user with the same storage path.

## Connect with a PAT

Set `AKB_PAT` in the host's environment or pass `--pat <token>`. Either
selects PAT mode. To declare the mode in your host configuration, pass
`--auth-mode pat` with the PAT input. Use `AKB_MCP_URL` to supply the MCP
URL through the environment.

A revoked or expired PAT requires a replacement token and a proxy restart.
PAT mode reads the PAT for each request and works on installations without an
OAuth server.
