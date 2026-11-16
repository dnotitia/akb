// Requests the Home first-run guide hands to an AI app, and the links that open
// the app with a request already in its composer. The app never sends a
// request on its own: the person reviews it and presses Send.
//
// Commands come from `mcp-snippets` so the shape of every install command still
// lives in one place.

import type { AuthConfig } from "./api";
import { akbMcpArgs, mcpInstallSnippets, mcpOAuthSnippets } from "./mcp-snippets";

/** AI apps the first-run guide can open with a prepared request. */
export type HandoffApp = "claude-code" | "codex" | "claude-desktop";

export const HANDOFF_APP_LABELS: Record<HandoffApp, string> = {
  "claude-code": "Claude Code",
  codex: "Codex",
  "claude-desktop": "Claude Desktop",
};

/** How the AI app authenticates to AKB. */
export type HandoffCredential = { kind: "oauth"; signInWith: string } | { kind: "key"; token: string };

/** Opens a new Codex chat (Codex app or the Codex mode of the ChatGPT app) with `prompt` in the composer. */
export function codexNewChatLink(prompt: string): string {
  return `codex://new?prompt=${encodeURIComponent(prompt)}`;
}

/** Opens a new Code session in the Claude desktop app with `prompt` in the composer. */
export function claudeCodeNewSessionLink(prompt: string): string {
  return `claude://code/new?q=${encodeURIComponent(prompt)}`;
}

/** Opens a new chat in the Claude desktop app with `prompt` in the composer. */
export function claudeNewChatLink(prompt: string): string {
  return `claude://claude.ai/new?q=${encodeURIComponent(prompt)}`;
}

/** Where a request for `app` runs. Claude Desktop is configured from a Claude Code session. */
export function setupLink(app: HandoffApp, prompt: string): string {
  return app === "codex" ? codexNewChatLink(prompt) : claudeCodeNewSessionLink(prompt);
}

/** Where requests that use AKB run once `app` is connected. */
export function followUpLink(app: HandoffApp, prompt: string): string {
  if (app === "codex") return codexNewChatLink(prompt);
  if (app === "claude-code") return claudeCodeNewSessionLink(prompt);
  return claudeNewChatLink(prompt);
}

/**
 * Whether `app` can sign in to AKB through the deployment's OAuth sign-in.
 * Claude Desktop chats reach remote servers from Anthropic's cloud, so they
 * always use the local proxy with a connection key.
 */
export function supportsSignIn(app: HandoffApp, config: AuthConfig): boolean {
  return app !== "claude-desktop" && config.available && config.mcp_oauth.enabled;
}

/** The sign-in people will see in the browser: an external identity provider, else the AKB account. */
export function signInLabel(config: AuthConfig): string {
  const external = config.providers.find(provider => provider.provider_type !== "local-realm");
  return external?.display_name ?? "your AKB account";
}

const KEEP_EXISTING = "If an MCP server named akb already exists, do not change it. Tell me instead.";
const NO_KEY_ECHO = "Do not repeat the connection key in your reply.";

/** The request that connects `app` to this AKB. */
export function setupRequest(app: HandoffApp, credential: HandoffCredential): string {
  if (app === "claude-desktop") {
    if (credential.kind !== "key") throw new Error("Claude Desktop connects with a connection key.");
    return claudeDesktopRequest(credential.token);
  }
  const label = HANDOFF_APP_LABELS[app];
  const agent = app === "codex" ? "codex" : "claude";
  const lines = [`Connect AKB, our team knowledge base, to ${label}.`];
  if (credential.kind === "oauth") {
    lines.push(`1. Run: ${mcpOAuthSnippets()[agent]}`);
    if (app === "claude-code") {
      lines.push(
        "2. Then run: claude mcp login akb. If it fails because there is no terminal, run it in a real terminal instead: "
          + "on macOS or Linux run `script -q /dev/null claude mcp login akb`; "
          + "on Windows open a new window with `Start-Process claude -ArgumentList \"mcp login akb\"`.",
        `3. A browser window opens. I will sign in with ${credential.signInWith} within 5 minutes. Wait until the sign-in finishes.`,
      );
    } else {
      lines.push(`2. A browser window opens. I will sign in with ${credential.signInWith}. Wait until the sign-in finishes.`);
    }
    lines.push(`${lines.length}. ${KEEP_EXISTING}`);
  } else {
    lines.push(
      `1. Run: ${mcpInstallSnippets(credential.token)[agent]}`,
      "2. The command needs Node.js with npx. If npx is missing, or akb-mcp reports an unsupported Node.js version, "
        + "install the current Node.js LTS release and run it again.",
      `3. ${KEEP_EXISTING}`,
      `4. ${NO_KEY_ECHO}`,
    );
  }
  const next = app === "codex" ? "chat" : "session";
  lines.push(`When it is done, reply only: "AKB is connected. Start a new ${next} to use it."`);
  return lines.join("\n");
}

function claudeDesktopRequest(token: string): string {
  const args = akbMcpArgs(token);
  const mac = JSON.stringify({ command: "<full path to npx>", args, env: { PATH: "<folder that contains node>:/usr/bin:/bin" } });
  const windows = JSON.stringify({ command: "cmd", args: ["/c", "npx", ...args] });
  return [
    "Set up AKB, our team knowledge base, for chats in the Claude desktop app.",
    "1. Check that `npx akb-mcp --help` runs. If Node.js or npx is missing, or akb-mcp reports an unsupported Node.js version, "
      + "install the current Node.js LTS release (macOS: Homebrew if available, otherwise nvm; Windows: `winget install OpenJS.NodeJS.LTS`), then check again.",
    "2. Open the Claude desktop configuration file. macOS: ~/Library/Application Support/Claude/claude_desktop_config.json. "
      + "Windows: %APPDATA%\\Claude\\claude_desktop_config.json. If it does not exist, create it with {\"mcpServers\": {}}.",
    `3. Add an "akb" entry under mcpServers and keep every other entry. ${KEEP_EXISTING}`,
    `   macOS: ${mac}`,
    `   Windows: ${windows}`,
    "   The desktop app does not read your shell profile, so on macOS use full paths.",
    `4. ${NO_KEY_ECHO}`,
    "When it is done, reply only: \"Quit and reopen the Claude app to use AKB in your chats.\"",
  ].join("\n");
}

/** The request that creates the person's first vault once the app is connected. */
export function firstVaultRequest(): string {
  return [
    "Create my first vault in AKB.",
    "1. Call akb_whoami to get my username, then create a vault named \"<username>-notes\" with the description \"Personal notes\".",
    "2. Add a README document that explains what the vault is for.",
    "3. Reply with the vault name and its address.",
  ].join("\n");
}

/** Requests worth trying once the first vault exists. */
export function tryRequests(vault: string): { title: string; request: string }[] {
  return [
    {
      title: "See what you can already read",
      request: "List the AKB vaults I can read and tell me in one line what each one is for.",
    },
    {
      title: "Ask a question across AKB",
      request: "Ask me what I want to know first. Then search AKB, answer briefly, and list the documents you used.",
    },
    {
      title: `Start a note in ${vault}`,
      request: `Ask me for three things I want to keep track of. Then save them as a document titled "Getting started" in my AKB vault ${vault}.`,
    },
  ];
}
