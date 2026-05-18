import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { RotateCcw } from "lucide-react";
import { SkillBadge } from "@/components/ui/skill-badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { getSkillTemplate, updateDocument } from "@/lib/api";

interface Props {
  vault: string;
  docId: string;
}

export function SkillBanner({ vault, docId }: Props) {
  const queryClient = useQueryClient();
  const [resetOpen, setResetOpen] = useState(false);

  async function handleReset() {
    const template = await getSkillTemplate();
    const content = template.replace("{vault}", vault);
    await updateDocument(vault, docId, { content });
    queryClient.invalidateQueries({ queryKey: ["document", vault, docId] });
    queryClient.invalidateQueries({ queryKey: ["vault-skill-preview", vault] });
  }

  return (
    <div className="flex items-center justify-between h-9 px-3 border-b border-border bg-surface">
      <div className="flex items-center gap-3">
        <SkillBadge defined />
        <span className="font-serif italic text-[13px] text-foreground-muted">
          Agents writing into this vault read this first.
        </span>
      </div>
      <Button
        variant="ghost"
        size="sm"
        onClick={() => setResetOpen(true)}
      >
        <RotateCcw className="h-3 w-3" aria-hidden />
        Reset to template
      </Button>

      <ConfirmDialog
        open={resetOpen}
        onOpenChange={setResetOpen}
        title="Reset vault skill?"
        description="Replace current content with the AKB-default template? Previous content stays in git history."
        confirmLabel="Reset"
        variant="destructive"
        onConfirm={handleReset}
      />
    </div>
  );
}
