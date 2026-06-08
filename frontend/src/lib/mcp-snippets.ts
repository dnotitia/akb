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
