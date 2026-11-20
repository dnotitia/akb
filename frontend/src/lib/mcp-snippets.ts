export type McpAgent = "claude" | "cursor" | "codex" | "vscode" | "openclaw";
export type McpTransport = "stdio" | "http";
export type McpAuthentication = "oauth" | "pat";

type McpRecipe =
  | { transport: McpTransport; authentication: "oauth"; endpoint: string }
  | { transport: McpTransport; authentication: "pat"; endpoint: string; pat: string };

export const MCP_AGENT_LABELS: Record<McpAgent, string> = {
  claude: "Claude Code",
  cursor: "Cursor",
  codex: "Codex",
  vscode: "VS Code",
  openclaw: "OpenClaw",
};

export const MCP_SNIPPET_LABELS: Record<McpAgent, string> = {
  claude: "Terminal · Claude Code",
  cursor: "mcp.json",
  codex: "Terminal · Codex",
  vscode: "mcp.json",
  openclaw: "openclaw.json",
};

function shellArgument(value: string): string {
  return /^[a-zA-Z0-9_./:@%=-]+$/.test(value) ? value : "'" + value.replaceAll("'", "'\"'\"'") + "'";
}

function json(config: object): string {
  return JSON.stringify(config, null, 2);
}

type HttpOAuthPreset = { render: (endpoint: string) => string; signIn: string };
type McpSnippet = { code: string; signIn?: string };

const RECIPES: Record<McpAgent, {
  stdio: (args: string[]) => string;
  http: { oauth?: HttpOAuthPreset; pat?: (endpoint: string, pat: string) => string };
}> = {
  claude: {
    stdio: args => `claude mcp add --scope user akb -- npx ${args.map(shellArgument).join(" ")}`,
    http: { oauth: {
      render: endpoint => `claude mcp add --scope user --transport http akb ${shellArgument(endpoint)}`,
      signIn: "Open Claude Code, enter /mcp, select AKB and complete the browser sign-in. Return to Claude Code after approving access.",
    } },
  },
  cursor: {
    stdio: args => json({ mcpServers: { akb: { command: "npx", args } } }),
    http: {
      oauth: {
        render: endpoint => json({ mcpServers: { akb: { url: endpoint } } }),
        signIn: "In Cursor's MCP settings, select AKB and complete the browser sign-in when prompted.",
      },
      pat: (endpoint, pat) => json({ mcpServers: { akb: { url: endpoint, headers: { Authorization: `Bearer ${pat}` } } } }),
    },
  },
  codex: {
    stdio: args => `codex mcp add akb -- npx ${args.map(shellArgument).join(" ")}`,
    http: {},
  },
  vscode: {
    stdio: args => json({ servers: { akb: { type: "stdio", command: "npx", args } } }),
    http: { oauth: {
      render: endpoint => json({ servers: { akb: { type: "http", url: endpoint } } }),
      signIn: "Follow VS Code's AKB sign-in prompt and approve access in your browser.",
    } },
  },
  openclaw: {
    stdio: args => json({ mcp: { servers: { akb: { command: "npx", args } } } }),
    http: {},
  },
};

export function hasMcpRecipe(agent: McpAgent, transport: McpTransport, authentication: McpAuthentication): boolean {
  return transport === "stdio" || Boolean(RECIPES[agent].http[authentication]);
}

export function supportsSecureMcpEndpoint(endpoint: string): boolean {
  const url = new URL(endpoint);
  return url.protocol === "https:" || (url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname));
}

export function mcpLoginCommand(endpoint: string): string {
  return `npx akb-mcp login --url ${shellArgument(endpoint)}`;
}

export function mcpSnippet(agent: McpAgent, recipe: McpRecipe): McpSnippet | undefined {
  const { endpoint } = recipe;
  const presets = RECIPES[agent];
  if (recipe.transport === "stdio") {
    const args = ["akb-mcp", "--url", endpoint];
    if (recipe.authentication === "pat") args.push("--pat", recipe.pat);
    return { code: presets.stdio(args) };
  }
  if (recipe.authentication === "oauth") {
    const preset = presets.http.oauth;
    return preset && { code: preset.render(endpoint), signIn: preset.signIn };
  }
  const render = presets.http.pat;
  return render && { code: render(endpoint, recipe.pat) };
}
