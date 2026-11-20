import { execFileSync } from "node:child_process";
import { describe, expect, it } from "vitest";
import { hasMcpRecipe, mcpLoginCommand, mcpSnippet, type McpAgent } from "../mcp-snippets";

const endpoint = "https://akb.example.test/team/$group&docs's/(ref);/mcp/";
const pat = "sample'$(literal)";

function shellWords(command: string | undefined): string[] {
  const output = execFileSync("sh", ["-c", `printf '%s\\0' ${command}`], { encoding: "utf8" });
  return output.split("\0").slice(0, -1);
}

describe("connection recipes", () => {
  it("quotes literal URL paths and secrets in terminal commands", () => {
    expect(shellWords(mcpLoginCommand(endpoint))).toEqual(["npx", "akb-mcp", "login", "--url", endpoint]);
    expect(shellWords(mcpSnippet("claude", { transport: "http", authentication: "oauth", endpoint })?.code))
      .toEqual(["claude", "mcp", "add", "--scope", "user", "--transport", "http", "akb", endpoint]);
    expect(shellWords(mcpSnippet("codex", { transport: "stdio", authentication: "pat", endpoint, pat })?.code))
      .toEqual(["codex", "mcp", "add", "akb", "--", "npx", "akb-mcp", "--url", endpoint, "--pat", pat]);
  });

  it.each(["claude", "cursor", "codex", "vscode", "openclaw"] as const)("has both helper recipes for %s", agent => {
    expect(hasMcpRecipe(agent, "stdio", "oauth")).toBe(true);
    expect(hasMcpRecipe(agent, "stdio", "pat")).toBe(true);
    const oauth = mcpSnippet(agent, { transport: "stdio", authentication: "oauth", endpoint })?.code;
    expect(oauth).toContain("/mcp/");
    expect(oauth).not.toMatch(/--pat|--insecure|Bearer/);
  });

  it.each([
    ["claude", true, false],
    ["cursor", true, true],
    ["codex", false, false],
    ["vscode", true, false],
    ["openclaw", false, false],
  ] satisfies [McpAgent, boolean, boolean][])("offers only the tested direct recipes for %s", (agent, oauth, key) => {
    expect(hasMcpRecipe(agent, "http", "oauth")).toBe(oauth);
    expect(hasMcpRecipe(agent, "http", "pat")).toBe(key);
  });
});
