import { useEffect, useMemo, useState } from "react";
import { Trash2 } from "lucide-react";
import {
  forgetCategory,
  forgetMemory,
  recallMemories,
  type Memory,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { EmptyState } from "@/components/empty-state";
import { timeAgo } from "@/lib/utils";

const CATEGORIES = ["all", "context", "preference", "learning", "work", "general"] as const;
type Cat = (typeof CATEGORIES)[number];

export function MemoryTab() {
  const [active, setActive] = useState<Cat>("all");
  const [memories, setMemories] = useState<Memory[] | null>(null);
  const [error, setError] = useState("");
  const [pendingForget, setPendingForget] = useState<Memory | null>(null);
  const [pendingPurgeCategory, setPendingPurgeCategory] = useState<string | null>(null);

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);

  async function refresh() {
    try {
      const r = await recallMemories(active === "all" ? undefined : active);
      setMemories(r.memories || []);
      setError("");
    } catch (e: any) {
      setError(e?.message || "Failed to load");
      setMemories([]);
    }
  }

  // Per-category counts come from the unfiltered memory list, so the chip
  // strip stays useful even after filtering. Re-fetched on active="all".
  const allCounts = useMemo(() => {
    if (!memories || active !== "all") return null;
    const c: Record<string, number> = {};
    for (const m of memories) c[m.category] = (c[m.category] || 0) + 1;
    return c;
  }, [memories, active]);

  return (
    <div className="space-y-6">
      <div>
        <p className="text-sm text-foreground-muted leading-relaxed max-w-prose mb-4">
          Persistent memories your agents recorded across sessions — preferences,
          learnings, ongoing work context. Inspect what's stored and remove
          anything that shouldn't be there.
        </p>

        {/* Category filter strip */}
        <div className="flex flex-wrap items-center gap-1.5">
          {CATEGORIES.map((c) => {
            const isActive = active === c;
            const count = allCounts?.[c];
            return (
              <button
                key={c}
                type="button"
                onClick={() => setActive(c)}
                aria-pressed={isActive}
                className={`inline-flex items-baseline gap-1.5 px-2.5 h-9 text-xs font-mono uppercase tracking-wider transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background ${
                  isActive
                    ? "bg-foreground text-background border border-foreground"
                    : "border border-border text-foreground-muted hover:text-foreground hover:bg-surface-muted"
                }`}
              >
                {c}
                {count !== undefined && (
                  <span className="tabular-nums">{count}</span>
                )}
              </button>
            );
          })}
          {active !== "all" && memories && memories.length > 0 && (
            <Button
              size="sm"
              variant="outline"
              className="ml-auto"
              onClick={() => setPendingPurgeCategory(active)}
            >
              <Trash2 className="h-4 w-4" aria-hidden />
              Forget all in {active}
            </Button>
          )}
        </div>
      </div>

      {error ? (
        <div role="alert" className="border border-destructive p-3 text-sm">
          <span className="coord-spark mb-1 block text-destructive">⚠ FAILED</span>
          {error}
        </div>
      ) : memories === null ? (
        <div className="coord px-3 py-8">— LOADING —</div>
      ) : memories.length === 0 ? (
        <EmptyState
          title="No memories"
          description={
            active === "all"
              ? "Once an agent calls akb_remember or finishes a session, memories appear here."
              : `Nothing stored in "${active}" yet.`
          }
        />
      ) : (
        <ol className="border border-border bg-surface divide-y divide-border">
          {memories.map((m) => (
            <li
              key={m.memory_id}
              className="grid grid-cols-[88px_minmax(0,1fr)_auto_auto] items-baseline gap-x-4 gap-y-1 px-4 py-3"
            >
              <span className="coord-ink truncate">{m.category.toUpperCase()}</span>
              <p className="text-sm text-foreground leading-relaxed whitespace-pre-line break-words min-w-0">
                {m.content}
              </p>
              <span
                className="coord tabular-nums w-[64px] text-right"
                title={m.created_at}
              >
                {timeAgo(m.updated_at || m.created_at)}
              </span>
              <button
                type="button"
                onClick={() => setPendingForget(m)}
                aria-label="Forget this memory"
                className="inline-flex items-center gap-1 px-2 h-7 text-xs font-mono uppercase tracking-wider text-foreground-muted hover:text-destructive transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
              >
                <Trash2 className="h-3 w-3" aria-hidden />
                Forget
              </button>
            </li>
          ))}
        </ol>
      )}

      <ConfirmDialog
        open={pendingForget !== null}
        onOpenChange={(o) => !o && setPendingForget(null)}
        title="Forget this memory?"
        description={
          pendingForget
            ? `${pendingForget.content.slice(0, 200)}${pendingForget.content.length > 200 ? "…" : ""}\n\nThis cannot be undone.`
            : ""
        }
        confirmLabel="Forget"
        variant="destructive"
        onConfirm={async () => {
          if (!pendingForget) return;
          await forgetMemory(pendingForget.memory_id);
          await refresh();
        }}
      />
      <ConfirmDialog
        open={pendingPurgeCategory !== null}
        onOpenChange={(o) => !o && setPendingPurgeCategory(null)}
        title={
          pendingPurgeCategory
            ? `Forget all "${pendingPurgeCategory}" memories?`
            : ""
        }
        description="Every memory in this category will be removed. This cannot be undone."
        confirmLabel="Forget all"
        variant="destructive"
        onConfirm={async () => {
          if (!pendingPurgeCategory) return;
          await forgetCategory(pendingPurgeCategory);
          await refresh();
        }}
      />
    </div>
  );
}
