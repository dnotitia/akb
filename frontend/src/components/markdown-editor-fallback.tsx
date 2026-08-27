import { LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";

// Separate module so the Suspense fallback ships in the main bundle —
// importing it from `markdown-editor.tsx` would pull in the full Plate
// chunk before the editor is needed, defeating the lazy split.
export function MarkdownEditorFallback() {
  return (
    <LoadingState label="Loading editor" className="min-h-[300px] border border-border bg-surface px-5 py-5">
      <div className="space-y-4">
        <Skeleton className="h-8 w-52 rounded-[var(--radius-md)]" />
        <Skeleton className="h-4 w-full rounded-[var(--radius-sm)]" />
        <Skeleton className="h-4 w-11/12 rounded-[var(--radius-sm)]" />
        <Skeleton className="h-4 w-4/5 rounded-[var(--radius-sm)]" />
        <Skeleton className="mt-7 h-6 w-2/5 rounded-[var(--radius-md)]" />
        <Skeleton className="h-4 w-full rounded-[var(--radius-sm)]" />
      </div>
    </LoadingState>
  );
}
