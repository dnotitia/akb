import { useMemo, useState } from "react";
import { Rocket, Plus, Copy, Check, Eye, EyeOff } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { CodeSnippet } from "@/components/ui/code-snippet";
import { createPAT } from "@/lib/api";
import { mcpInstallSnippets, MCP_AGENT_LABELS, MCP_AGENT_FILES, type McpAgent } from "@/lib/mcp-snippets";

export const QUICKSTART_DISMISS_KEY = "akb.quickstartDismissed";

/**
 * First-run quickstart. Shown once (on the dashboard) when the user has no
 * Personal Access Tokens yet: mint a token + copy the per-agent install
 * command, so a fresh account is one paste away from a connected agent.
 */
export function QuickstartDialog({
  open,
  onOpenChange,
  onTokenCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onTokenCreated?: () => void;
}) {
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);
  const [mintError, setMintError] = useState<string | null>(null);
  const [pat, setPat] = useState<string | null>(null);
  const [showPat, setShowPat] = useState(true);
  const [copied, setCopied] = useState(false);
  const [agent, setAgent] = useState<McpAgent>("claude");

  const snippets = useMemo(() => mcpInstallSnippets(pat || "<YOUR_PAT>"), [pat]);

  async function mint() {
    setMintError(null);
    setCreating(true);
    try {
      const r = await createPAT(name.trim() || "agent-token");
      setPat(r.token);
      onTokenCreated?.();
    } catch (e) {
      // No app-wide toast system — surface the failure inline or the user just
      // sees the button settle with no token and no explanation.
      setMintError(e instanceof Error ? e.message : "Couldn't mint a token. Please try again.");
    }
    setCreating(false);
  }

  async function copyPat() {
    if (!pat) return;
    // clipboard is undefined on insecure origins — guard so copying a
    // show-once token never throws silently.
    try {
      await navigator.clipboard?.writeText(pat);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard blocked — token stays visible to copy manually */
    }
  }

  function dontShowAgain() {
    localStorage.setItem(QUICKSTART_DISMISS_KEY, "1");
    onOpenChange(false);
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <div className="flex items-center gap-3">
            <span className="feature-tile feat-knowledge" style={{ width: 40, height: 40 }}>
              <Rocket size={20} strokeWidth={1.75} />
            </span>
            <div>
              <DialogTitle>Connect your first agent</DialogTitle>
              <DialogDescription>
                Mint an access token, then paste one line into your coding agent — that's the whole setup.
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        {/* Step 1 — mint */}
        <div className="space-y-2">
          <div className="coord-spark">Step 1 · Mint a token</div>
          {pat ? (
            <div
              className="rounded-[var(--radius-md)] border border-accent/40 bg-accent/5 p-3"
              role="status"
              aria-live="polite"
            >
              <div className="coord-spark mb-1.5">Fresh token — copy it now (shown once)</div>
              <div className="flex items-center gap-2">
                <code className="flex-1 font-mono text-xs text-foreground break-all leading-snug">
                  {showPat ? pat : pat.slice(0, 12) + "•".repeat(16)}
                </code>
                {/* full token always reachable by SR even while masked */}
                {!showPat && <span className="sr-only">Token value: {pat}</span>}
                <button
                  onClick={() => setShowPat((v) => !v)}
                  aria-label={showPat ? "Hide token" : "Show token"}
                  className="text-foreground-muted hover:text-primary cursor-pointer shrink-0 rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                >
                  {showPat ? <EyeOff className="h-4 w-4" aria-hidden /> : <Eye className="h-4 w-4" aria-hidden />}
                </button>
                <button
                  onClick={copyPat}
                  aria-label={copied ? "Token copied" : "Copy token"}
                  className="text-foreground-muted hover:text-primary cursor-pointer shrink-0 rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                >
                  {copied ? <Check className="h-4 w-4 text-success" aria-hidden /> : <Copy className="h-4 w-4" aria-hidden />}
                </button>
              </div>
            </div>
          ) : (
            <>
              <div className="flex gap-2">
                <Input
                  placeholder="Token name (e.g. my-laptop)"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && mint()}
                  aria-invalid={mintError ? true : undefined}
                />
                <Button variant="accent" onClick={mint} loading={creating} className="shrink-0">
                  {!creating && <Plus className="h-4 w-4" aria-hidden />}
                  Mint token
                </Button>
              </div>
              {mintError && (
                <Alert variant="destructive" className="text-xs">{mintError}</Alert>
              )}
            </>
          )}
        </div>

        {/* Step 2 — install snippet */}
        <div className="space-y-2">
          <div className="coord-spark">Step 2 · Add it to your agent</div>
          <Tabs value={agent} onValueChange={(v) => setAgent(v as McpAgent)}>
            <TabsList className="flex-wrap">
              {(Object.keys(MCP_AGENT_LABELS) as McpAgent[]).map((a) => (
                <TabsTrigger key={a} value={a} className="text-xs">{MCP_AGENT_LABELS[a]}</TabsTrigger>
              ))}
            </TabsList>
            <TabsContent value={agent} className="pt-2">
              <CodeSnippet code={snippets[agent]} filename={MCP_AGENT_FILES[agent]} />
              {!pat && (
                <p className="mt-2 coord">Mint a token above to drop a ready-to-paste command.</p>
              )}
            </TabsContent>
          </Tabs>
        </div>

        <DialogFooter className="sm:justify-between">
          <Button variant="ghost" size="sm" onClick={dontShowAgain}>Don't show again</Button>
          <Button variant="default" size="sm" onClick={() => onOpenChange(false)}>Close</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
