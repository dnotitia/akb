import { describe, expect, it } from "vitest";
import {
  claudeCodeNewSessionLink, claudeNewChatLink, codexNewChatLink, firstVaultRequest, followUpLink, setupLink,
  setupRequest, signInLabel, supportsSignIn, tryRequests,
} from "@/lib/ai-app-handoff";
import { AUTH_CONFIG_UNAVAILABLE, type AuthConfig } from "@/lib/api";
import { MCP_URL } from "@/lib/mcp-snippets";

const sso: AuthConfig = {
  available: true, schema_version: 2, auth_mode: "sso",
  local_auth: { enabled: false }, keycloak: { enabled: true, browser_session_ready: true },
  providers: [
    { provider_type: "oidc", alias: "workforce", display_name: "Company SSO", login_url: "/api/v1/auth/sso/workforce/login" },
    { provider_type: "local-realm", alias: "local", display_name: "This workspace", login_url: "/api/v1/auth/sso/local/login" },
  ],
  mcp_oauth: { enabled: true },
};

const decode = (link: string) => decodeURIComponent(link.slice(link.indexOf("=") + 1));

describe("AI app hand-off", () => {
  it("opens each app with the request in its composer", () => {
    const prompt = "Say \"hi\" & list vaults\nthen stop";
    expect(codexNewChatLink(prompt)).toMatch(/^codex:\/\/new\?prompt=/);
    expect(claudeCodeNewSessionLink(prompt)).toMatch(/^claude:\/\/code\/new\?q=/);
    expect(claudeNewChatLink(prompt)).toMatch(/^claude:\/\/claude\.ai\/new\?q=/);
    for (const link of [codexNewChatLink(prompt), claudeCodeNewSessionLink(prompt), claudeNewChatLink(prompt)]) {
      expect(link).not.toContain(" ");
      expect(decode(link)).toBe(prompt);
    }
  });

  it("sets Claude Desktop up from Claude Code and uses its chat afterwards", () => {
    expect(setupLink("claude-desktop", "x")).toMatch(/^claude:\/\/code\/new/);
    expect(followUpLink("claude-desktop", "x")).toMatch(/^claude:\/\/claude\.ai\/new/);
    expect(setupLink("codex", "x")).toMatch(/^codex:\/\/new/);
    expect(followUpLink("claude-code", "x")).toMatch(/^claude:\/\/code\/new/);
  });

  it("names the sign-in people will see, falling back to the AKB account", () => {
    expect(signInLabel(sso)).toBe("Company SSO");
    expect(signInLabel({ ...sso, providers: sso.providers.filter(p => p.provider_type === "local-realm") })).toBe("your AKB account");
  });

  it("offers sign-in only where the deployment and the app support it", () => {
    expect(supportsSignIn("codex", sso)).toBe(true);
    expect(supportsSignIn("claude-code", sso)).toBe(true);
    expect(supportsSignIn("claude-desktop", sso)).toBe(false);
    expect(supportsSignIn("codex", { ...sso, mcp_oauth: { enabled: false } })).toBe(false);
    expect(supportsSignIn("codex", AUTH_CONFIG_UNAVAILABLE)).toBe(false);
  });

  it("connects Codex with OAuth in one command", () => {
    const request = setupRequest("codex", { kind: "oauth", signInWith: "Company SSO" });
    expect(request).toContain(`codex mcp add akb --url ${MCP_URL}`);
    expect(request).toContain("sign in with Company SSO");
    expect(request).toContain("do not change it");
    expect(request).not.toContain("--pat");
  });

  it("runs the Claude Code sign-in through a real terminal when needed", () => {
    const request = setupRequest("claude-code", { kind: "oauth", signInWith: "Company SSO" });
    expect(request).toContain(`claude mcp add --scope user --transport http akb ${MCP_URL}`);
    expect(request).toContain("claude mcp login akb");
    expect(request).toContain("script -q /dev/null claude mcp login akb");
    expect(request).toContain("Start-Process claude");
    expect(request).toContain("within 5 minutes");
    expect(request).toMatch(/^4\. If an MCP server named akb already exists/m);
  });

  it("embeds a connection key without asking the app to repeat it", () => {
    for (const app of ["codex", "claude-code"] as const) {
      const request = setupRequest(app, { kind: "key", token: "akb_secret_value" });
      expect(request).toContain("--pat akb_secret_value");
      expect(request).toContain("Do not repeat the connection key");
      expect(request).toContain("Node.js");
    }
  });

  it("configures Claude Desktop chats with the local helper and a key only", () => {
    const request = setupRequest("claude-desktop", { kind: "key", token: "akb_secret_value" });
    expect(request).toContain("claude_desktop_config.json");
    expect(request).toContain("%APPDATA%\\Claude\\claude_desktop_config.json");
    expect(request).toContain("\"akb_secret_value\"");
    expect(request).toContain("winget install OpenJS.NodeJS.LTS");
    expect(() => setupRequest("claude-desktop", { kind: "oauth", signInWith: "Company SSO" })).toThrow();
  });

  it("asks for the first vault and suggests next requests for it", () => {
    expect(firstVaultRequest()).toContain("akb_whoami");
    const next = tryRequests("me-notes");
    expect(next).toHaveLength(3);
    expect(next.some(item => item.request.includes("me-notes"))).toBe(true);
  });
});
