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
  const [error, setError] = useState<string | null>(null);

  // Read the doc from the shared TanStack cache (same key as DocumentView's
  // useQuery + VaultSkillPage's useQuery), so the edit dialog hydrates from
  // the latest server state without a duplicate fetch.
  const docQuery = useQuery({
    queryKey: ["document", vault, docId],
    queryFn: () => getDocument(vault, docId),
    retry: false,
    enabled: false,
  });
  const doc = docQuery.data;

  async function handleReset() {
    setError(null);
    try {
      const template = await getSkillTemplate();
      const content = template.replaceAll("{vault}", vault);
      await updateDocument(vault, docId, { content });
      queryClient.invalidateQueries({ queryKey: ["document", vault, docId] });
      queryClient.invalidateQueries({ queryKey: ["vault-skill-preview", vault] });
    } catch (e: any) {
      setError(e?.message || "Reset failed");
      throw e;
    }
  }

  return (
    <div className="flex items-center justify-between h-9 px-3 border-b border-border bg-surface">
      <div className="flex items-center gap-3">
        <SkillBadge defined />
        <span className="font-serif italic text-[13px] text-foreground-muted">
          Agents writing into this vault read this first.
        </span>
        {error && (
          <p role="alert" className="coord text-destructive ml-1">{error}</p>
        )}
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
