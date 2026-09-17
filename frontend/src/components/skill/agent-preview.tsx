import { useQuery } from "@tanstack/react-query";
import { getVaultSkillPreview } from "@/lib/api";
import { Alert } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import { LoadingState } from "@/components/ui/loading-state";

/**
 * The vault guide exactly as an agent receives it — the server-composed
 * GET /help/vault-skill-preview/{vault} text, not the stored markdown.
 * Lives beside the guide editor in vault settings; the document viewer
 * has no skill surface.
 */
export function AgentPreview({ vault }: { vault: string }) {
  const helpQuery = useQuery({
    queryKey: ["vault-skill-preview", vault],
    queryFn: () => getVaultSkillPreview(vault),
    retry: false,
  });
  if (helpQuery.isLoading) {
    return (
      <LoadingState label="Loading agent preview" className="p-4">
        <Skeleton className="h-64 w-full rounded-[var(--radius-lg)]" />
      </LoadingState>
    );
  }
  if (helpQuery.isError)
    return (
      <Alert variant="destructive" className="m-4">
        Failed to load agent preview.
      </Alert>
    );
  return (
    <pre className="font-mono text-[11px] leading-snug whitespace-pre-wrap bg-background border border-border rounded-[var(--radius-lg)] p-4">
      {helpQuery.data}
    </pre>
  );
}
