// Single source of truth for the "connect your agent" install snippets.
// Used by the dashboard CONNECT panel, Settings, and the first-run Quickstart
// — keep the command shape in one place so a change lands everywhere.

export type McpAgent = "claude" | "cursor" | "codex" | "vscode" | "openclaw";

export const MCP_AGENT_LABELS: Record<McpAgent, string> = {
  claude: "Claude Code",
  cursor: "Cursor",
  codex: "Codex",
  vscode: "VS Code",
  openclaw: "OpenClaw",
};

/** Filename / context label shown on each snippet's header bar. */
export const MCP_AGENT_FILES: Record<McpAgent, string> = {
  claude: "terminal",
  cursor: "mcp.json",
  codex: "terminal",
  vscode: "mcp.json",
  openclaw: "openclaw.json",
};

/** The AKB MCP endpoint the local `akb-mcp` proxy connects to. */
export const MCP_URL = `${window.location.origin}/mcp/`;

/** Per-agent install snippet wiring `akb-mcp` to this instance with a PAT. */
export function mcpInstallSnippets(pat: string): Record<McpAgent, string> {
  const args = ["akb-mcp", "--url", MCP_URL, "--pat", pat, "--insecure"];
  const cmd = `npx ${args.join(" ")}`;
  return {
    claude: `claude mcp add --scope user akb -- ${cmd}`,
    cursor: JSON.stringify({ mcpServers: { akb: { command: "npx", args } } }, null, 2),
    codex: `codex mcp add akb -- ${cmd}`,
    vscode: JSON.stringify({ servers: { akb: { type: "stdio", command: "npx", args } } }, null, 2),
    openclaw: JSON.stringify({ mcp: { servers: { akb: { command: "npx", args } } } }, null, 2),
  };
}

/**
 * Per-agent install snippet for the OAuth Resource Server path — no PAT
 * required, the agent does Dynamic Client Registration + an OAuth 2.1
 * authorization-code flow against the AKB-configured OIDC provider (Keycloak
 * in the reference deployment) and stores the resulting tokens itself.
 *
 * Only meaningful when the backend has `mcp_oauth_enabled = true`. Surfaced
 * from `/api/v1/auth/config.mcp_oauth.enabled` so the UI can pick which
 * snippet to render.
 *
 * Agents that don't support remote-HTTP MCP at all (e.g. stdio-only
 * clients) are not present in the result — callers should treat keys as
 * optional and fall back to the PAT snippet.
 */
export function mcpOAuthSnippets(): Partial<Record<McpAgent, string>> {
  return {
    // Claude Code's `mcp add` registers the server config; the separate
    // `mcp login` opens a browser for the OAuth flow. The two-step shape
    // matches the documented Claude Code UX and produces a clear copy-
    // and-paste pair the user can run back-to-back.
    claude: [
      `claude mcp add --scope user --transport http akb ${MCP_URL}`,
      `claude mcp login akb`,
    ].join("\n"),
    cursor: JSON.stringify({ mcpServers: { akb: { url: MCP_URL } } }, null, 2),
    vscode: JSON.stringify({ servers: { akb: { type: "http", url: MCP_URL } } }, null, 2),
  };
}
