import { useMemo, useState } from "react";
import { Rocket, Plus, Loader2, Copy, Check, Eye, EyeOff } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
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
  const [pat, setPat] = useState<string | null>(null);
  const [showPat, setShowPat] = useState(true);
  const [copied, setCopied] = useState(false);
  const [agent, setAgent] = useState<McpAgent>("claude");

  const snippets = useMemo(() => mcpInstallSnippets(pat || "<YOUR_PAT>"), [pat]);

  async function mint() {
    setCreating(true);
    try {
      const r = await createPAT(name.trim() || "agent-token");
      setPat(r.token);
      onTokenCreated?.();
    } catch {
      /* surfaced by the error boundary */
    }
    setCreating(false);
  }

  function copyPat() {
    if (!pat) return;
    navigator.clipboard.writeText(pat);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
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
            <div className="rounded-[var(--radius-md)] border border-accent/40 bg-accent/5 p-3">
              <div className="coord-spark mb-1.5">Fresh token — copy it now (shown once)</div>
              <div className="flex items-center gap-2">
                <code className="flex-1 font-mono text-xs text-foreground break-all leading-snug">
                  {showPat ? pat : pat.slice(0, 12) + "•".repeat(16)}
                </code>
                <button onClick={() => setShowPat((v) => !v)} aria-label={showPat ? "Hide" : "Show"} className="text-foreground-muted hover:text-accent cursor-pointer shrink-0">
                  {showPat ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
                <button onClick={copyPat} aria-label="Copy token" className="text-foreground-muted hover:text-accent cursor-pointer shrink-0">
                  {copied ? <Check className="h-4 w-4 text-success" /> : <Copy className="h-4 w-4" />}
                </button>
              </div>
            </div>
          ) : (
            <div className="flex gap-2">
              <Input
                placeholder="Token name (e.g. my-laptop)"
                value={name}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && mint()}
              />
              <Button variant="accent" onClick={mint} disabled={creating} className="shrink-0">
                {creating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
                Mint token
              </Button>
            </div>
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
