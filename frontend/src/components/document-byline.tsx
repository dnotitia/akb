import { CheckCircle2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { DocumentTimestamp } from "@/components/document-reading-controls";

/** Read-only metadata, deliberately outside the authored Markdown contract. */
export function DocumentByline({ author, updatedAt, timestampLabel = "Last edited", status, historical = false, version, saved = false, compact = false }: {
  author?: string; updatedAt?: string | null; timestampLabel?: string; status?: string;
  historical?: boolean; version?: string; saved?: boolean; compact?: boolean;
}) {
  return <div role="group" aria-label="Document metadata" data-slot="document-byline" className={compact ? "document-byline document-byline-compact" : "document-byline"}>
    {author && <span className="document-byline-author"><span>Author</span><span>{author}</span></span>}
    <DocumentTimestamp value={updatedAt} label={timestampLabel} compact />
    {!historical && status === "draft" && <Badge variant="draft">Draft</Badge>}
    {!historical && status === "archived" && <Badge variant="archived">Archived</Badge>}
    {version && <Badge variant="outline">Version {version.slice(0, 8)}</Badge>}
    {saved && <span role="status" className="document-byline-saved"><CheckCircle2 size={13} aria-hidden />Saved</span>}
  </div>;
}
