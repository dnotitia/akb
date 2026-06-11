import { cn, formatDate, isFresh, timeAgo } from "@/lib/utils";

interface RelativeTimeProps {
  /** ISO timestamp to render relatively (e.g. "3h ago", "8mo ago"). */
  iso?: string | null;
  /** Layout classes for the call site (width, alignment). Applied to both
   *  the fresh and the muted variant so they align identically. */
  className?: string;
  /** Rendered when `iso` is missing. Defaults to timeAgo's "-". */
  fallback?: string;
}

/**
 * The single relative-time chip used everywhere a timestamp appears — the
 * grammar the Home "Recent activity" card established: a just-touched item
 * (<1h) gets the warm spark dot + spark-tinted time; everything older reads
 * as a muted `coord` label. Extracted so the dashboard recent feed, the vault
 * directory rows, the vault overview header, and the per-vault recent list all
 * speak ONE time format instead of each re-rolling timeAgo() inline.
 *
 * Pass alignment/width via `className` (e.g. "justify-end text-right w-[56px]").
 */
export function RelativeTime({ iso, className, fallback }: RelativeTimeProps) {
  if (!iso) {
    return <span className={cn("coord tabular-nums", className)}>{fallback ?? "-"}</span>;
  }
  // Exact date on hover, everywhere — the relative grain is the glance, the
  // tooltip is the precise answer.
  const title = formatDate(iso);
  if (isFresh(iso)) {
    return (
      <span
        title={title}
        className={cn(
          "inline-flex items-center gap-1 text-[11px] font-medium tabular-nums text-spark",
          className,
        )}
      >
        <span className="h-1.5 w-1.5 rounded-full bg-spark" aria-hidden />
        {timeAgo(iso)}
      </span>
    );
  }
  return (
    <span title={title} className={cn("coord tabular-nums", className)}>
      {timeAgo(iso)}
    </span>
  );
}
