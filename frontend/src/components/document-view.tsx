import { readWithAccessBoundary } from "@/lib/query-access-boundary";
import { Button } from "@/components/ui/button";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { getDocument } from "@/lib/api";
import { MarkdownRender } from "@/components/markdown-render";
import { Alert } from "@/components/ui/alert";
import { DocumentCopyButton, DocumentReadModes, DocumentTimestamp } from "@/components/document-reading-controls";
import { ResourceCommandRow } from "@/components/resource-command-row";
import { LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { useAccessVerification, useCurrentUser } from "@/contexts/current-user-context";

type ViewMode = "rendered" | "raw";

interface DocumentViewProps {
  vault: string;
  docId: string;
  /** Controlled view — provide this + onViewChange to sync with URL params */
  view?: ViewMode;
  onViewChange?: (next: ViewMode) => void;
  /**
   * Optional git commit hash. When set, the body is fetched at that
   * commit via getDocument(..., version) and the queryKey carries the
   * hash so commit-log / history selections render the historical body
   * instead of HEAD. Parent (DocumentPage) reads it from ?commit= URL
   * state; uncontrolled callers (VaultSkillPage) omit it and get HEAD.
   */
  version?: string;
  /** Framed file-viewer treatment used by the Vault document workspace. */
  appearance?: "plain" | "file";
  /** The resource shell owns commands; render only the reading surface. */
  bodyOnly?: boolean;
  idPrefix?: string;
}

/**
 * Self-sufficient doc body: fetches the document, renders the
 * rendered/raw segmented control, and shows the markdown content.
 *
 * Query key includes document, vault, docId, version, user, and access revision.
 * It matches DocumentPage
 * exactly so TanStack Query dedupes when both are mounted. Without
 * `version` in the key, historical-view URLs would render HEAD because
 * the un-versioned key collides with DocumentPage's versioned fetch
 * and serves whichever landed first.
 *
 * view/onViewChange are optional: when omitted, DocumentView manages
 * its own local toggle state (useful for embeds). When provided, the
 * caller drives view state (DocumentPage uses this to sync ?view= URL
 * params).
 */
export function DocumentView({
  vault,
  docId,
  view: viewProp,
  onViewChange,
  version,
  appearance = "plain",
  bodyOnly = false,
  idPrefix = "docview",
}: DocumentViewProps) {
  const queryClient = useQueryClient();
  const user = useCurrentUser();
  const { checking, revision } = useAccessVerification();
  const [localView, setLocalView] = useState<ViewMode>("rendered");

  // Controlled vs. uncontrolled view mode
  const view = viewProp ?? localView;
  const setView = (next: ViewMode) => {
    if (onViewChange) {
      onViewChange(next);
    } else {
      setLocalView(next);
    }
  };

  const { data: doc, isLoading, isFetching, error, refetch } = useQuery({
    queryKey: ["document", vault, docId, version, user?.user_id, revision],
    queryFn: ({ queryKey, signal }) => readWithAccessBoundary(queryClient, queryKey, () => getDocument(vault, docId, version), signal),
    enabled: !!vault && !!docId && !checking,
    retry: false,
    // The page already owns this query. Mounting its body must not immediately
    // refetch the response that just replaced the page skeleton.
    refetchOnMount: !bodyOnly,
  });

  if (isLoading) {
    return <DocumentViewLoading appearance={appearance} bodyOnly={bodyOnly} />;
  }

  if (!doc) {
    return (
      <Alert variant="destructive" className="my-4">
        Couldn't load this document body.
        {error instanceof Error ? ` ${error.message}` : ""}
        <Button variant="outline" size="sm" loading={isFetching} onClick={() => void refetch()}>Retry document body</Button>
      </Alert>
    );
  }

  const fileAppearance = appearance === "file";

  return (
    <section
      aria-label="Document content"
      className={cn(
        "document-reading-container",
        fileAppearance && !bodyOnly &&
          "overflow-hidden rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm",
      )}
    >
      {!bodyOnly && <ResourceCommandRow appearance="reader" className="document-unified-toolbar" meta={
        <div className="document-reader-status">{!version && <DocumentTimestamp value={doc.updated_at} compact />}</div>
      }>
        <div role="group" aria-label="Document reading tools" className="document-reader-controls">
          <DocumentReadModes view={view} onChange={setView} idPrefix={idPrefix} contentId={`${idPrefix}-content`} />
        </div>
        <DocumentCopyButton key={`${vault}:${docId}:${version ?? "latest"}`} content={doc.content || ""} />
      </ResourceCommandRow>}

      {!bodyOnly && error && <Alert variant="destructive">Document could not be updated. Previous content shown. <Button size="sm" variant="outline" loading={isFetching} onClick={() => void refetch()}>Retry document body</Button></Alert>}

      {/* ── Doc body ──────────────────────────────────────────────── */}
      {view === "rendered" ? (
        <div
          id={`${idPrefix}-content`}
          role="region"
          aria-label="Document preview"
          className="document-reading-inset min-w-0"
          style={{ maxWidth: "100%" }}
        >
          <MarkdownRender
            markdown={doc.content || ""}
            summary={doc.summary}
            className="document-reading-flow"
            assetContext={{
              mode: "authenticated",
              vault,
              document: doc.path,
              commit: doc.current_commit,
            }}
          />
        </div>
      ) : (
        <div
          id={`${idPrefix}-content`}
          role="region"
          aria-label="Raw markdown source"
          className="document-reading-inset relative bg-surface"
        >
          <pre
            data-testid="doc-raw"
            className="document-reading-source m-0 wrap-anywhere overflow-x-auto whitespace-pre-wrap font-mono text-[13px] leading-[1.65] text-foreground"
          >
            {doc.content || ""}
          </pre>
        </div>
      )}
    </section>
  );
}

function DocumentViewLoading({ appearance, bodyOnly }: Pick<DocumentViewProps, "appearance" | "bodyOnly">) {
  return (
    <LoadingState
      label="Loading document body"
      delayMs={200}
      className={cn(
        "document-reading-container",
        appearance === "file" && !bodyOnly &&
          "min-h-[28rem] overflow-hidden rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm",
      )}
    >
      <div className={cn(appearance !== "file" && "py-4")}>
        {!bodyOnly && <div className="flex min-h-11 items-center gap-2 border-b border-border bg-surface-2/60 px-3">
          <Skeleton className="h-7 w-24 rounded-[var(--radius-sm)]" />
          <Skeleton className="h-7 w-16 rounded-[var(--radius-sm)]" />
          <Skeleton className="ml-auto h-3 w-28 rounded-[var(--radius-sm)]" />
        </div>}
        <div className="document-reading-inset">
          <div className="document-reading-source space-y-4">
            <Skeleton className="h-8 w-3/5 rounded-[var(--radius-md)]" />
            <Skeleton className="h-4 w-full rounded-[var(--radius-sm)]" />
            <Skeleton className="h-4 w-11/12 rounded-[var(--radius-sm)]" />
            <Skeleton className="h-4 w-4/5 rounded-[var(--radius-sm)]" />
            <Skeleton className="mt-7 h-6 w-2/5 rounded-[var(--radius-md)]" />
            <Skeleton className="h-4 w-full rounded-[var(--radius-sm)]" />
            <Skeleton className="h-4 w-5/6 rounded-[var(--radius-sm)]" />
          </div>
        </div>
      </div>
    </LoadingState>
  );
}
