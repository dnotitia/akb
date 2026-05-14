import { useState } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";

/**
 * SummaryFold — collapsed-by-default rendering of an auto-generated
 * document summary.
 *
 * AKB's `documents.summary` is produced by `metadata_worker` for
 * search relevance, not human readability — when shown at body-text
 * weight it looks like the document's lead paragraph and obscures the
 * real content beneath. This component demotes it visually (smaller,
 * muted, italic) and clamps it to the first two lines, with an
 * explicit toggle to read the rest.
 *
 * For short summaries (≤ FOLD_THRESHOLD chars, roughly two lines at
 * typical column widths) the toggle is hidden — there's nothing to
 * unfold and the chrome would be more noise than the text it sits next to.
 */

const FOLD_THRESHOLD = 140;

interface Props {
  summary?: string | null;
  /** Slightly larger leading for the public-share page hero context. */
  prominent?: boolean;
  /** Outer spacing. Defaults to a small block; pass "" to flush against neighbors. */
  className?: string;
}

export function SummaryFold({ summary, prominent = false, className = "my-4" }: Props) {
  const [open, setOpen] = useState(false);
  if (!summary) return null;
  const needsFold = summary.length > FOLD_THRESHOLD;

  const textCls = prominent
    ? "font-serif-italic text-foreground-muted text-[15px] leading-[1.6] max-w-prose"
    : "font-serif-italic text-foreground-muted text-sm leading-[1.55] max-w-prose";

  if (!needsFold) {
    return (
      <p className={`${textCls} ${className}`}>
        {summary}
      </p>
    );
  }

  return (
    <div className={className}>
      <p className={`${textCls} ${open ? "" : "line-clamp-2"}`}>
        {summary}
      </p>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="mt-1 inline-flex items-center gap-1 coord text-foreground-muted hover:text-foreground transition-colors"
      >
        {open ? <ChevronUp className="h-3 w-3" aria-hidden /> : <ChevronDown className="h-3 w-3" aria-hidden />}
        {open ? "less" : "more"}
      </button>
    </div>
  );
}
