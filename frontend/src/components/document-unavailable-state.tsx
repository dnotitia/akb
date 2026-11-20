import { useId, type ReactNode } from "react";
import { CircleAlert, FileQuestion, LockKeyhole } from "lucide-react";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Panel } from "@/components/ui/panel";

/** No document identity or server error text belongs on an unavailable reader. */
export function DocumentUnavailableState({ error, retrying, onRetry, recovery, headingLevel = 2, compact = false, retryLabel }: {
  error: unknown;
  retrying: boolean;
  onRetry: () => void;
  recovery?: ReactNode;
  headingLevel?: 1 | 2;
  compact?: boolean;
  retryLabel?: string;
}) {
  const id = useId();
  const status = error instanceof ApiError ? error.status : undefined;
  const restricted = status === 401 || status === 403 || status === 404;
  const Heading = headingLevel === 1 ? "h1" : "h2";
  const Icon = status === 401 || status === 403 ? LockKeyhole : status === 404 ? FileQuestion : CircleAlert;
  const title = status === 401 ? "Sign in to view this document"
    : status === 403 ? "You don’t have access to this document"
      : status === 404 ? "Document unavailable"
        : "Couldn’t load this document";
  const description = status === 401 ? "Your session may have expired. Sign in again to check your access."
    : status === 403 ? "Ask the Vault owner or the person who shared this link to give you access. If access was just granted, check again."
      : status === 404 ? "This link may have moved, been removed, or be outside your access. Check the link with the person who shared it."
        : "A connection or server problem may be preventing this document from loading. Try again in a moment.";
  const retry = <Button variant={restricted ? "outline" : "default"} loading={retrying} onClick={onRetry}>
    {retryLabel ?? (restricted ? "Check access again" : "Retry document")}
  </Button>;

  return <Panel role="alert" aria-labelledby={`${id}-title`} aria-describedby={`${id}-description`}
    data-slot="document-unavailable" variant="workspace" flush inset={false}
    className={cn("mx-auto w-full max-w-lg p-5 sm:p-6", compact && "p-4 sm:p-4")}>
    <div className="mb-4 flex h-10 w-10 items-center justify-center rounded-[var(--radius-md)] bg-surface-muted text-foreground-muted">
      <Icon className="h-5 w-5" aria-hidden />
    </div>
    <Heading id={`${id}-title`} className={cn("text-xl font-semibold leading-7 tracking-tight text-foreground", compact && "text-base leading-6")}>{title}</Heading>
    <p id={`${id}-description`} className="mt-2 text-sm leading-relaxed text-foreground-muted">{description}</p>
    <div className="mt-5 flex flex-wrap gap-2 [&_button]:max-sm:min-h-11 [&_a]:max-sm:min-h-11 [@media(pointer:coarse)]:[&_button]:min-h-11 [@media(pointer:coarse)]:[&_a]:min-h-11">
      {restricted && recovery}
      {retry}
      {!restricted && recovery}
    </div>
  </Panel>;
}
