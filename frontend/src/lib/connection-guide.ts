import type { McpAgent, McpAuthentication, McpTransport } from "./mcp-snippets";

export const CONNECTION_GUIDES: Record<McpAgent, { introduction: string; location: string; add: string; check: string }> = {
  claude: {
    introduction: "Claude Code is the developer app you run from a terminal. Claude Desktop and Claude on the web have separate connection settings.",
    location: "Open a terminal on the computer where you use Claude Code.",
    add: "Paste the command below and press Enter. It adds AKB to your Claude Code settings.",
    check: "In Claude Code, enter /mcp and check that AKB is enabled. Then send the request below in the conversation.",
  },
  codex: {
    introduction: "Use the Codex command-line app on this computer.",
    location: "Open a terminal on the computer where you use Codex.",
    add: "Paste the command below and press Enter. It adds AKB to your Codex MCP settings.",
    check: "Open Codex and check its MCP server list for AKB. Start a new conversation if needed, then send the request below.",
  },
  cursor: {
    introduction: "Connect the Cursor editor you use on this computer.",
    location: "Open Cursor’s Customize page and find MCP. For a personal setup, open ~/.cursor/mcp.json in the editor.",
    add: "Add the akb entry under mcpServers using the configuration below. Keep the other server entries. Save the file and enable AKB in Cursor.",
    check: "Check that AKB is enabled in Cursor's MCP settings, then send the request below in its AI chat.",
  },
  vscode: {
    introduction: "Connect VS Code with an AI chat that supports MCP servers.",
    location: "In VS Code, open the Command Palette and run MCP: Open User Configuration.",
    add: "Add the akb entry under servers using the configuration below. Keep existing entries, save the file, then start AKB from VS Code's MCP server list.",
    check: "Open VS Code's MCP server list and check that AKB is running, then send the request below in AI chat.",
  },
  openclaw: {
    introduction: "Connect the OpenClaw installation that runs your agent.",
    location: "Open the OpenClaw configuration file (openclaw.json) on the computer where it runs.",
    add: "Add the akb entry under mcp.servers using the configuration below. Keep existing entries, save the file and reload OpenClaw.",
    check: "Check that AKB is available in OpenClaw, then send the request below in its conversation.",
  },
};

export const CONNECTION_TEST_PROMPT = "Use AKB to list the vaults I can access. Do not create, edit or delete anything.";

export const TRANSPORT_LABELS: Record<McpTransport, string> = {
  stdio: "Local helper (stdio)",
  http: "Direct connection (HTTP)",
};

export const AUTHENTICATION_LABELS: Record<McpAuthentication, string> = {
  oauth: "Sign in with AKB (OAuth/SSO)",
  pat: "Personal connection key (PAT)",
};
