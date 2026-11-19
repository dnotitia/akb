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

function shellArgument(value: string): string {
  return /^[a-zA-Z0-9_./:@%=-]+$/.test(value) ? value : "'" + value.replaceAll("'", "'\"'\"'") + "'";
}

export function supportsMcpProxyEndpoint(endpoint: string): boolean {
  const url = new URL(endpoint);
  return url.protocol === "https:" || (url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname));
}

export function mcpInstallSnippets(endpoint: string, pat: string): Record<McpAgent, string> {
  const args = ["akb-mcp", "--url", endpoint, "--pat", pat];
  const cmd = `npx ${args.map(shellArgument).join(" ")}`;
  return {
    claude: `claude mcp add --scope user akb -- ${cmd}`,
    cursor: JSON.stringify({ mcpServers: { akb: { command: "npx", args } } }, null, 2),
    codex: `codex mcp add akb -- ${cmd}`,
    vscode: JSON.stringify({ servers: { akb: { type: "stdio", command: "npx", args } } }, null, 2),
    openclaw: JSON.stringify({ mcp: { servers: { akb: { command: "npx", args } } } }, null, 2),
  };
}

export function mcpOAuthSnippets(endpoint: string): Partial<Record<McpAgent, string>> {
  return {
    // Register in the terminal; authenticate using /mcp inside Claude Code.
    claude: `claude mcp add --scope user --transport http akb ${shellArgument(endpoint)}`,
    cursor: JSON.stringify({ mcpServers: { akb: { url: endpoint } } }, null, 2),
    vscode: JSON.stringify({ servers: { akb: { type: "http", url: endpoint } } }, null, 2),
  };
}
