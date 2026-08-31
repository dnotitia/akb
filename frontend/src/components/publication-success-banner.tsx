import { Link } from "react-router-dom";
import { ExternalLink, X } from "lucide-react";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import type { Publication } from "@/lib/api";

export function PublicationSuccessBanner({
  vault,
  publication,
  resourceLabel,
  onDismiss,
}: {
  vault: string;
  publication: Publication;
  resourceLabel: "File" | "Table";
  onDismiss: () => void;
}) {
  return (
    <div className="flex shrink-0 flex-col gap-2 border-b border-border bg-surface px-3 py-2 sm:flex-row sm:items-center sm:px-4 lg:px-5">
      <Alert
        variant="success"
        title={`${resourceLabel} published`}
        className="min-w-0 flex-1 border-0 bg-transparent p-0"
      >
        Anyone with the link can open the public, read-only view.
      </Alert>
      <div className="flex shrink-0 items-center gap-1 self-end sm:self-auto">
        <Button asChild variant="outline" size="sm">
          <a href={publication.share_url} target="_blank" rel="noreferrer">
            Open public link
            <ExternalLink className="h-3.5 w-3.5" aria-hidden />
          </a>
        </Button>
        <Button asChild variant="ghost" size="sm">
          <Link to={`/vault/${encodeURIComponent(vault)}/publications`}>
            Manage links
          </Link>
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label="Dismiss publication notice"
          onClick={onDismiss}
        >
          <X className="h-4 w-4" aria-hidden />
        </Button>
      </div>
    </div>
  );
}
