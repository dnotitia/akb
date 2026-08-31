import { ExternalLink } from "lucide-react";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  titleConflictLocation,
  type DocumentTitleConflict,
} from "@/lib/document-title-conflict";

interface DocumentTitleConflictNoticeProps {
  conflict: DocumentTitleConflict;
  onOpenExisting?: () => void;
  onChooseAlternative: () => void;
  chooseAlternativeLabel: string;
  onKeepBoth: () => void;
  keepBothLabel: string;
  keepingBoth?: boolean;
}

export function DocumentTitleConflictNotice({
  conflict,
  onOpenExisting,
  onChooseAlternative,
  chooseAlternativeLabel,
  onKeepBoth,
  keepBothLabel,
  keepingBoth = false,
}: DocumentTitleConflictNoticeProps) {
  return (
    <Alert
      variant="warning"
      title={
        conflict.exactContent
          ? "This document already exists"
          : `“${conflict.title}” already exists here`
      }
    >
      <p>
        {conflict.exactContent
          ? `The same title and content are already saved in ${titleConflictLocation(conflict.collection)}.`
          : `A document with this exact title is already in ${titleConflictLocation(conflict.collection)}.`}
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        {onOpenExisting && (
          <Button type="button" size="sm" onClick={onOpenExisting}>
            <ExternalLink className="h-3.5 w-3.5" aria-hidden />
            Open existing
          </Button>
        )}
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={onChooseAlternative}
          disabled={keepingBoth}
        >
          {chooseAlternativeLabel}
        </Button>
        <Button
          type="button"
          variant="link"
          size="sm"
          onClick={onKeepBoth}
          loading={keepingBoth}
        >
          {keepBothLabel}
        </Button>
      </div>
    </Alert>
  );
}
