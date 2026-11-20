import type { ReactNode } from "react";
import type { McpAuthentication, McpTransport } from "@/lib/mcp-snippets";

export function ConnectionTroubleshooting({ authentication, transport, loginCommand }: {
  authentication?: McpAuthentication | null;
  transport?: McpTransport;
  loginCommand?: string | null;
}) {
  const signIn = loginCommand
    ? <>Run <code className="break-all">{loginCommand}</code> again and wait for it to finish. Reload or reconnect AKB in your tool after signing in.</>
    : transport === "http"
      ? "Open the AI tool’s MCP settings and use its sign-in control for AKB. Reconnect in the tool after completing sign-in."
      : "For a local helper, run the terminal sign-in command in Add AKB. For a direct connection, use the AI tool’s own MCP sign-in control.";
  const topics: (readonly [string, ReactNode])[] = [
      ["The command does not run", "Check that your AI tool is installed. The local helper also needs a supported Node.js release and npx. Run terminal commands on the computer where your tool runs."],
      ...(authentication === "pat" ? [] : [
        ["Sign-in did not finish or expired", signIn],
        ["Sign-in was denied or registration failed", "If you cancelled or denied consent, restart sign-in using the same method. If the authorization server rejects client registration, ask your administrator to check the installation’s OAuth setup."],
      ] as const),
      ["AKB is already registered", "Open the AI tool’s existing AKB entry before adding another one. To switch how it connects, update that entry using your tool’s instructions."],
      ["The tool cannot reach AKB", "Check the network and VPN from the computer running your AI tool. Ask your administrator to verify the installation’s public MCP address and TLS certificate. Web sign-in does not test the tool’s network path."],
      ["A Vault is missing from the result", "Check the account used in the AI tool and ask the Vault owner to share access. An empty Vault list can be a successful connection with no accessible Vaults."],
      ...(authentication === "oauth" ? [] : [
        ["A key creation request was interrupted", "Before requesting another key, open Connection keys in AI connections and check whether the first request created one. Keep existing keys active until you have reviewed the result."],
        ["I only have part of a connection key", "The key list shows an identifying prefix. Enter the full key you saved, or open Connection keys to manage your keys."],
      ] as const),
  ];
  return <div className="connection-troubleshooting space-y-2 text-sm">
    {topics.map(([title, description]) => <details key={title} className="rounded-[var(--radius-md)] border border-border bg-surface">
      <summary className="cursor-pointer px-3 py-3 font-medium text-foreground focus-ring-instant">{title}</summary>
      <p className="px-3 pb-3 leading-relaxed text-foreground-muted">{description}</p>
    </details>)}
  </div>;
}
