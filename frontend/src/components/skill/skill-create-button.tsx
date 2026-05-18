import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { getSkillTemplate, putDocument } from "@/lib/api";

interface Props {
  vault: string;
  variant?: "accent" | "outline";
}

export function SkillCreateButton({ vault, variant = "accent" }: Props) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleClick() {
    setBusy(true);
    setError(null);
    try {
      const template = await getSkillTemplate();
      const content = template.replaceAll("{vault}", vault);
      await putDocument({
        vault,
        collection: "overview",
        title: "Guide",
        type: "skill",
        content,
        tags: ["akb:skill"],
      });
      queryClient.invalidateQueries({ queryKey: ["document", vault, "overview/vault-skill.md"] });
      queryClient.invalidateQueries({ queryKey: ["vault-skill-preview", vault] });
      navigate(`/vault/${vault}/skill`, { replace: true });
    } catch (e: any) {
      const raw = e?.message || "";
      setError(raw ? `Failed to create vault skill: ${raw}` : "Failed to create vault skill");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="inline-flex flex-col items-start gap-1">
      <Button
        size="sm"
        variant={variant}
        onClick={handleClick}
        disabled={busy}
      >
        <Sparkles className="h-3 w-3" aria-hidden />
        {busy ? "Creating…" : "Create from template"}
      </Button>
      {error && (
        <p role="alert" className="coord text-destructive">{error}</p>
      )}
    </div>
  );
}
