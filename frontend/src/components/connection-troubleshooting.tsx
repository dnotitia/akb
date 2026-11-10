export function ConnectionTroubleshooting() {
  return <div className="connection-troubleshooting space-y-2 text-sm">
    {[
      ["The command does not run", "Check that your AI tool is installed on this computer. For a connection-key setup, npx also needs a supported Node.js installation. Run the setup command in your terminal app."],
      ["The browser sign-in does not open", "Add AKB to the AI tool first, then use that tool's MCP sign-in control. Signing in to this AKB website does not complete the tool's authorization."],
      ["AKB is already registered", "Open the AI tool's existing AKB entry and review it before adding another one. To switch connection methods, update that entry using your tool's instructions."],
      ["The tool cannot reach AKB", "Check connectivity from the computer running your AI tool, including any required VPN. Claude web and Desktop remote connectors connect from Claude's cloud; they need their own network access. A browser check alone cannot prove that path works."],
      ["A Vault is missing from the result", "Check the account used in the AI tool and ask the Vault owner to share access. An empty Vault list can be a successful connection with no accessible Vaults."],
      ["A key creation request was interrupted", "Before requesting another key, open Connection keys in AI connections and check whether the first request created one. Keep existing keys active until you have reviewed the result."],
      ["I only have part of a connection key", "The key list shows an identifying prefix, not the full secret. Use the complete key you saved, or create a new one in AI connections. Replacing a key and revoking the old one are separate actions."],
    ].map(([title, description]) => <details key={title} className="rounded-[var(--radius-md)] border border-border bg-surface">
      <summary className="cursor-pointer px-3 py-3 font-medium text-foreground focus-ring-instant">{title}</summary>
      <p className="px-3 pb-3 leading-relaxed text-foreground-muted">{description}</p>
    </details>)}
  </div>;
}
