import { Loader2 } from "lucide-react";

// Separate module so the Suspense fallback ships in the main bundle —
// importing it from `markdown-editor.tsx` would pull in the full Plate
// chunk before the editor is needed, defeating the lazy split.
export function MarkdownEditorFallback() {
  return (
    <div className="min-h-[300px] border border-border bg-surface-muted px-5 py-4 coord">
      <Loader2 className="h-4 w-4 inline animate-spin mr-2" aria-hidden />
      Loading editor…
    </div>
  );
}
