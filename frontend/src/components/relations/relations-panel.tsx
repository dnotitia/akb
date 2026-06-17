import { useState } from "react";
import { Link } from "react-router-dom";
import { GitGraph, Link2, Loader2, Plus, Unlink } from "lucide-react";
import {
  RELATION_TYPES,
  type RelationType,
  type RelationRow,
  createRelation,
  deleteRelation,
} from "@/lib/api";
import { parseUri } from "@/lib/uri";
import { edgeFor, hrefFor } from "@/components/relations/relation-row-utils";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { SelectMenu } from "@/components/ui/select-menu";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { TooltipText } from "@/components/ui/tooltip-text";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ResourcePicker, type PickedResource } from "@/components/relations/resource-picker";

const RELATION_COLOR: Record<string, string> = {
  implements: "text-success",
  depends_on: "text-info",
  references: "text-info",
  related_to: "text-foreground-muted",
  attached_to: "text-success",
  derived_from: "text-warning",
};

const RELATION_LABEL: Record<RelationType, string> = {
  references: "References",
  related_to: "Related to",
  depends_on: "Depends on",
  implements: "Implements",
  derived_from: "Derived from",
  attached_to: "Attached to",
};

interface RelationsPanelProps {
  vault: string;
  /** The current document's canonical akb:// URI (the link source). */
  sourceUri: string;
  relations: RelationRow[];
  relationsError: boolean;
  graphHref: string;
  onReload: () => void;
}

export function RelationsPanel({
  vault,
  sourceUri,
  relations,
  relationsError,
  graphHref,
  onReload,
}: RelationsPanelProps) {
  const [addOpen, setAddOpen] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<RelationRow | null>(null);

  return (
    <div className="flex h-full flex-col">
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="coord">{relations.length} relation{relations.length === 1 ? "" : "s"}</span>
        <Button variant="ghost" size="sm" onClick={() => setAddOpen(true)}>
          <Plus className="h-3.5 w-3.5" aria-hidden />
          Add
        </Button>
      </div>

      {relationsError ? (
        <Alert variant="destructive">Failed to load relations.</Alert>
      ) : relations.length === 0 ? (
        <div className="coord">No relations yet.</div>
      ) : (
        <ol className="space-y-0.5 font-mono text-[11px] leading-[1.9]">
          {relations.map((r) => {
            const label = r.name || parseUri(r.uri)?.id || r.uri;
            const relColor = RELATION_COLOR[r.relation] || "text-foreground-muted";
            // `links_to` is auto-derived from markdown — re-created on save, so
            // unlinking it is meaningless. Hide its delete affordance.
            const deletable = r.relation !== "links_to";
            return (
              <li key={`${r.direction}:${r.relation}:${r.uri}`} className="group flex items-center gap-1">
                <Link
                  to={hrefFor(r, vault)}
                  className="grid min-w-0 flex-1 grid-cols-[minmax(64px,88px)_1fr] gap-1.5 rounded-[var(--radius-sm)] px-1 py-0.5 transition-colors hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                >
                  <span className={relColor}>{r.relation || "relates"}</span>
                  <TooltipText tip={label} className="truncate text-foreground group-hover:text-link">
                    {r.direction === "incoming" ? "← " : "→ "}
                    {label}
                  </TooltipText>
                </Link>
                {deletable && (
                  <button
                    type="button"
                    onClick={() => setPendingDelete(r)}
                    aria-label={`Remove relation to ${label}`}
                    className="shrink-0 rounded-[var(--radius-sm)] p-1 text-foreground-muted opacity-0 transition-colors hover:text-destructive focus-visible:opacity-100 group-hover:opacity-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <Unlink className="h-3 w-3" aria-hidden />
                  </button>
                )}
              </li>
            );
          })}
        </ol>
      )}

      <Link
        to={graphHref}
        className="mt-3 inline-flex items-center gap-1 rounded-[var(--radius-sm)] text-xs text-link transition-colors hover:text-link-hover hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
      >
        <GitGraph className="h-3 w-3" aria-hidden /> Open in graph →
      </Link>

      <AddRelationDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        vault={vault}
        sourceUri={sourceUri}
        onCreated={onReload}
      />

      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(o) => !o && setPendingDelete(null)}
        title="Remove relation?"
        description={
          pendingDelete
            ? `This removes the "${pendingDelete.relation}" link to ${
                pendingDelete.name || parseUri(pendingDelete.uri)?.id || pendingDelete.uri
              }. The documents themselves are not affected.`
            : ""
        }
        confirmLabel="Remove"
        variant="destructive"
        onConfirm={async () => {
          if (!pendingDelete) return;
          const { source, target } = edgeFor(pendingDelete, sourceUri);
          await deleteRelation(source, target, pendingDelete.relation);
          setPendingDelete(null);
          onReload();
        }}
      />
    </div>
  );
}

interface AddRelationDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  vault: string;
  sourceUri: string;
  onCreated: () => void;
}

function AddRelationDialog({ open, onOpenChange, vault, sourceUri, onCreated }: AddRelationDialogProps) {
  const [relation, setRelation] = useState<RelationType>("references");
  const [target, setTarget] = useState<PickedResource | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function reset() {
    setRelation("references");
    setTarget(null);
    setError(null);
    setBusy(false);
  }

  async function submit() {
    if (!target) return;
    setBusy(true);
    setError(null);
    try {
      await createRelation(sourceUri, target.uri, relation);
      reset();
      onOpenChange(false);
      onCreated();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to create relation.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (!o) reset();
        onOpenChange(o);
      }}
    >
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Link2 className="h-4 w-4 text-foreground-muted" aria-hidden />
            Add relation
          </DialogTitle>
          <DialogDescription>
            Link this document to another in the same vault with a typed relation.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="space-y-1.5">
            <span className="coord">Relation</span>
            <SelectMenu
              value={relation}
              onValueChange={(v) => setRelation(v as RelationType)}
              aria-label="Relation type"
              options={RELATION_TYPES.map((t) => ({ value: t, label: RELATION_LABEL[t] }))}
            />
          </div>
          <div className="space-y-1.5">
            <span className="coord">Target document</span>
            <ResourcePicker
              vault={vault}
              excludeUri={sourceUri}
              value={target}
              onChange={setTarget}
            />
          </div>
          {error && <Alert variant="destructive">{error}</Alert>}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={busy}>
            Cancel
          </Button>
          <Button variant="accent" onClick={submit} disabled={!target || busy}>
            {busy && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
            Add relation
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
