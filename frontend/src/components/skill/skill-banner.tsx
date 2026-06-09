import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, RotateCcw } from "lucide-react";
import { SkillBadge } from "@/components/ui/skill-badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { FrontmatterEditDialog } from "@/components/frontmatter-edit-dialog";
import { getDocument, getSkillTemplate, updateDocument } from "@/lib/api";

interface Props {
  vault: string;
  docId: string;
}

export function SkillBanner({ vault, docId }: Props) {
  const queryClient = useQueryClient();
  const [resetOpen, setResetOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);

  // Self-fetch the doc on this key. DocumentView (the only mount point) fetches
  // a *different* 4-element key (["document", vault, docId, version]), so a
  // disabled query reading this 3-element key was never seeded — `doc` stayed
  // undefined and "Edit details" was disabled forever. The 3-element key here
  // is the prefix the invalidations below (and DocumentView's versioned key)
  // both match, so a refetch stays in sync. retry:false keeps a failure quiet.
  const docQuery = useQuery({
    queryKey: ["document", vault, docId],
    queryFn: () => getDocument(vault, docId),
    retry: false,
    enabled: !!(vault && docId),
  });
  const doc = docQuery.data;

  // Reset rejects on failure; the ConfirmDialog catches it and renders the
  // error inside the open dialog (one place), so the banner keeps no error
  // state of its own.
  async function handleReset() {
    const template = await getSkillTemplate();
    const content = template.replaceAll("{vault}", vault);
    await updateDocument(vault, docId, { content });
    queryClient.invalidateQueries({ queryKey: ["document", vault, docId] });
    queryClient.invalidateQueries({ queryKey: ["vault-skill-preview", vault] });
  }

  return (
    <div className="flex items-center justify-between h-9 px-3 border-b border-border bg-surface">
      <div className="flex items-center gap-3">
        <SkillBadge defined />
        <span className="italic font-semibold tracking-[-0.015em] text-sm text-foreground-muted">
          Agents writing into this vault read this first.
        </span>
      </div>
      <div className="flex items-center gap-1">
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setEditOpen(true)}
          disabled={!doc}
        >
          <Pencil className="h-3 w-3" aria-hidden />
          Edit details
        </Button>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setResetOpen(true)}
        >
          <RotateCcw className="h-3 w-3" aria-hidden />
          Reset to template
        </Button>
      </div>

      <ConfirmDialog
        open={resetOpen}
        onOpenChange={setResetOpen}
        title="Reset to template?"
        description="Replace current content with the AKB-default template? Previous content stays in git history."
        confirmLabel="Reset"
        variant="destructive"
        onConfirm={handleReset}
      />

      {doc && (
        <FrontmatterEditDialog
          open={editOpen}
          onOpenChange={setEditOpen}
          vault={vault}
          docId={docId}
          doc={doc}
          editBody
          onSaved={() => {
            queryClient.invalidateQueries({ queryKey: ["document", vault, docId] });
            queryClient.invalidateQueries({ queryKey: ["vault-skill-preview", vault] });
          }}
        />
      )}
    </div>
  );
}
