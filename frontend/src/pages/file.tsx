import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import {
  Clock3,
  Download,
  Eye,
  File,
  FolderTree,
  RefreshCw,
  UserRound,
} from "lucide-react";
import {
  FilePreviewBody,
} from "@/components/file-viewer";
import {
  ResourceCanvas,
  ResourceContextBar,
  ResourceViewerFrame,
  ResourceWorkspace,
  ResourceWorkspaceHeader,
} from "@/components/resource-workspace";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";
import { TooltipText } from "@/components/ui/tooltip-text";
import { authenticatedFetch } from "@/lib/api";
import {
  effectiveFileMime,
  filePreviewKind,
  formatFileSize,
} from "@/lib/file-preview";
import { parseFileUri } from "@/lib/uri";
import { timeAgo } from "@/lib/utils";

interface FileInfo {
  uri: string;
  name: string;
  collection?: string;
  description?: string;
  mime_type?: string;
  size_bytes?: number;
  created_by?: string;
  created_at?: string;
}

interface FileAccess {
  name?: string;
  download_url?: string;
  mime_type?: string;
  size_bytes?: number;
}

export default function FilePage() {
  const { name: vault, id: fileId } = useParams<{ name: string; id: string }>();
  const [info, setInfo] = useState<FileInfo | null>(null);
  const [access, setAccess] = useState<FileAccess | null>(null);
  const [loadError, setLoadError] = useState("");
  const [previewError, setPreviewError] = useState("");
  const [infoLoading, setInfoLoading] = useState(true);
  const [previewLoading, setPreviewLoading] = useState(true);

  useEffect(() => {
    if (!vault || !fileId) return;
    let cancelled = false;
    setInfo(null);
    setLoadError("");
    setInfoLoading(true);

    authenticatedFetch(`/api/v1/files/${encodeURIComponent(vault)}`)
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(`Couldn't load the file list (${response.status}).`);
        }
        return response.json().catch(() => null);
      })
      .then((data) => {
        if (cancelled || !data) return;
        const found = (data.items || []).find(
          (item: FileInfo) => parseFileUri(item.uri)?.id === fileId,
        );
        if (found) setInfo(found);
        else setLoadError("File not found in vault.");
      })
      .catch((error) => {
        if (!cancelled) setLoadError(error instanceof Error ? error.message : String(error));
      })
      .finally(() => {
        if (!cancelled) setInfoLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [vault, fileId]);

  const loadPreview = useCallback(async () => {
    if (!vault || !fileId) return;
    setPreviewLoading(true);
    setPreviewError("");
    try {
      const response = await authenticatedFetch(
        `/api/v1/files/${encodeURIComponent(vault)}/${encodeURIComponent(fileId)}/download`,
      );
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.download_url) {
        throw new Error(data.error || data.detail || `Preview unavailable (${response.status}).`);
      }
      setAccess(data);
    } catch (error) {
      setAccess(null);
      setPreviewError(error instanceof Error ? error.message : String(error));
    } finally {
      setPreviewLoading(false);
    }
  }, [vault, fileId]);

  useEffect(() => {
    void loadPreview();
  }, [loadPreview]);

  const displayName = info?.name || access?.name || fileId || "File";
  const mime = effectiveFileMime(
    info?.mime_type || access?.mime_type || "",
    displayName,
  );
  const kind = filePreviewKind(mime);
  const kindLabel = kind.charAt(0).toUpperCase() + kind.slice(1);
  const size = info?.size_bytes ?? access?.size_bytes;
  const creator = useMemo(() => readableCreator(info?.created_by), [info?.created_by]);

  if (infoLoading && !info && !access) {
    return <FilePageLoading />;
  }

  return (
    <ResourceWorkspace label="File workspace">
      <ResourceWorkspaceHeader
        icon={File}
        iconTone="file"
        title={displayName}
        subtitle={
          <>
            File <span aria-hidden>·</span>{" "}
            <span className="font-medium text-foreground">{vault}</span>
          </>
        }
        meta={<Badge variant="outline">{kindLabel}</Badge>}
        actions={
          access?.download_url ? (
            <Button asChild size="sm">
              <a
                href={access.download_url}
                target="_blank"
                rel="noreferrer"
                download={displayName}
              >
                <Download className="h-4 w-4" aria-hidden />
                <span className="hidden sm:inline">Download</span>
              </a>
            </Button>
          ) : (
            <Button
              type="button"
              variant="outline"
              size="sm"
              loading={previewLoading}
              onClick={() => void loadPreview()}
            >
              {!previewLoading && <RefreshCw className="h-4 w-4" aria-hidden />}
              <span className="hidden sm:inline">Retry preview</span>
            </Button>
          )
        }
      />

      <div className="relative min-h-0 flex-1 overflow-hidden">
        <ResourceCanvas>
          {loadError && !info ? (
            <div className="mx-auto w-full max-w-2xl pt-6">
              <Alert variant="destructive" title="File unavailable">
                {loadError}
              </Alert>
            </div>
          ) : (
            <>
              <ResourceContextBar
                trailing={
                  info?.created_at ? (
                    <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
                      <Clock3 className="h-3.5 w-3.5" aria-hidden />
                      {timeAgo(info.created_at)}
                    </span>
                  ) : undefined
                }
              >
                <div className="flex min-w-0 items-center gap-2 text-xs text-foreground-muted">
                  <FolderTree className="h-3.5 w-3.5 shrink-0 text-link" aria-hidden />
                  <span className="truncate">
                    <span className="font-medium text-foreground">
                      {info?.collection || "Root"}
                    </span>
                    <span aria-hidden> / </span>
                    <span className="font-mono">{displayName}</span>
                  </span>
                  {creator && (
                    <span className="hidden shrink-0 items-center gap-1.5 border-l border-border pl-3 xl:inline-flex">
                      <UserRound className="h-3.5 w-3.5" aria-hidden />
                      <span className="font-medium text-foreground">{creator}</span>
                    </span>
                  )}
                </div>
              </ResourceContextBar>

              <ResourceViewerFrame
                icon={Eye}
                label="File preview"
                meta={
                  <>
                    {info?.description && (
                      <TooltipText
                        tip={info.description}
                        className="hidden max-w-80 truncate xl:inline-block"
                      >
                        {info.description}
                      </TooltipText>
                    )}
                    <span className="hidden font-mono lg:inline">{mime || "Unknown format"}</span>
                    {size !== undefined && (
                      <span className="whitespace-nowrap tabular-nums">{formatFileSize(size)}</span>
                    )}
                  </>
                }
                bodyClassName="overflow-auto"
              >
                {previewLoading ? (
                  <FilePreviewLoading />
                ) : previewError || !access?.download_url ? (
                  <div className="mx-auto flex min-h-64 max-w-xl items-center px-4 py-10">
                    <Alert variant="warning" title="Preview unavailable" className="w-full">
                      <div className="space-y-3">
                        <p>{previewError || "The original file URL was not returned."}</p>
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          onClick={() => void loadPreview()}
                        >
                          <RefreshCw className="h-4 w-4" aria-hidden />
                          Retry
                        </Button>
                      </div>
                    </Alert>
                  </div>
                ) : (
                  <FilePreviewBody
                    key={access.download_url}
                    mime={mime}
                    directUrl={access.download_url}
                    rawUrl={access.download_url}
                    name={displayName}
                  />
                )}
              </ResourceViewerFrame>
            </>
          )}
        </ResourceCanvas>
      </div>
    </ResourceWorkspace>
  );
}

function FilePageLoading() {
  return (
    <LoadingState
      label="Loading file"
      className="flex h-full min-h-0 flex-col overflow-hidden bg-background"
    >
      <div className="flex h-full min-h-0 flex-col overflow-hidden">
        <header className="flex h-16 shrink-0 items-center gap-3 border-b border-border bg-surface px-3 sm:px-4 lg:px-5">
          <Skeleton className="hidden h-9 w-9 shrink-0 rounded-[var(--radius-md)] sm:block" />
          <div className="min-w-0 flex-1 space-y-2">
            <Skeleton className="h-5 w-2/3 max-w-56 rounded-[var(--radius-sm)]" />
            <Skeleton className="h-3 w-1/2 max-w-36 rounded-[var(--radius-sm)]" />
          </div>
          <Skeleton className="h-8 w-24 rounded-[var(--radius-md)]" />
        </header>
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden p-2 sm:p-3">
          <Skeleton className="mb-3 h-11 w-full rounded-[var(--radius-lg)]" />
          <div className="flex min-h-80 flex-1 flex-col overflow-hidden rounded-[var(--radius-lg)] border border-border bg-surface">
            <div className="flex h-11 shrink-0 items-center gap-3 border-b border-border bg-surface-2/60 px-3">
              <Skeleton className="h-4 w-28 rounded-[var(--radius-sm)]" />
              <Skeleton className="ml-auto h-3 w-32 rounded-[var(--radius-sm)]" />
            </div>
            <FilePreviewLoading />
          </div>
        </div>
      </div>
    </LoadingState>
  );
}

function FilePreviewLoading() {
  return (
    <LoadingState label="Loading file preview" className="min-h-64 flex-1 p-4 sm:p-6">
      <Skeleton className="h-full min-h-56 w-full rounded-[var(--radius-md)]" />
    </LoadingState>
  );
}

function readableCreator(value?: string): string | null {
  if (!value) return null;
  if (/^[0-9a-f]{8}-[0-9a-f-]{27,}$/i.test(value)) return null;
  return value;
}
