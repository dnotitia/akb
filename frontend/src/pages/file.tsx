import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Download,
  Info,
  RefreshCw,
} from "lucide-react";
import {
  FilePreviewBody,
} from "@/components/file-viewer";
import {
  ResourceCanvas,
  ResourceWorkspace,
} from "@/components/resource-workspace";
import { ResourceCommandRow } from "@/components/resource-command-row";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";
import { TooltipText } from "@/components/ui/tooltip-text";
import { ResourceActionsMenu } from "@/components/resource-actions-menu";
import { ResourceDeleteDialog } from "@/components/resource-delete-dialog";
import { PublicationSuccessBanner } from "@/components/publication-success-banner";
import { PublishOptionsDialog } from "@/components/publish-options-dialog";
import { useVaultRefresh } from "@/contexts/vault-refresh-context";
import { useAccessVerification, useCurrentUser } from "@/contexts/current-user-context";
import { usePublishResourceLocation } from "@/contexts/resource-location-context";
import {
  authenticatedFetch,
  deleteVaultFile,
  getVaultInfo,
  type Publication,
} from "@/lib/api";
import {
  effectiveFileMime,
  filePreviewKind,
  formatFileSize,
} from "@/lib/file-preview";
import { canEdit } from "@/lib/roles";
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
  const user = useCurrentUser();
  const { checking, revision } = useAccessVerification();
  return <FilePageContent key={`${user?.user_id ?? ""}:${revision}:${vault}:${fileId}`} accessChecking={checking} />;
}

function FilePageContent({ accessChecking }: { accessChecking: boolean }) {
  const { name: vault, id: fileId } = useParams<{ name: string; id: string }>();
  const navigate = useNavigate();
  const { refetchTree } = useVaultRefresh();
  const [info, setInfo] = useState<FileInfo | null>(null);
  const [access, setAccess] = useState<FileAccess | null>(null);
  const [loadError, setLoadError] = useState("");
  const [previewError, setPreviewError] = useState("");
  const [infoLoading, setInfoLoading] = useState(true);
  const [previewLoading, setPreviewLoading] = useState(true);
  const [canDelete, setCanDelete] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [publishOpen, setPublishOpen] = useState(false);
  const [published, setPublished] = useState<Publication | null>(null);

  useEffect(() => {
    if (!vault) return;
    let cancelled = false;
    setCanDelete(false);
    getVaultInfo(vault)
      .then((data) => {
        if (!cancelled) {
          setCanDelete(
            canEdit(data?.role) && !data?.is_archived && !data?.is_external_git,
          );
        }
      })
      .catch(() => {
        if (!cancelled) setCanDelete(false);
      });
    return () => {
      cancelled = true;
    };
  }, [vault]);

  useEffect(() => {
    if (!vault || !fileId) return;
    let cancelled = false;
    setInfo(null);
    setLoadError("");
    setInfoLoading(true);

    // Resolve the File by id. This used to read the vault listing and search
    // the page that came back, so any File outside that window reported itself
    // as missing however reachable it actually was.
    authenticatedFetch(
      `/api/v1/files/${encodeURIComponent(vault)}/${encodeURIComponent(fileId)}`,
    )
      .then(async (response) => {
        if (response.status === 404) {
          throw new Error("File not found in vault.");
        }
        if (!response.ok) {
          throw new Error(`Couldn't load the file (${response.status}).`);
        }
        return response.json().catch(() => null);
      })
      .then((data) => {
        if (cancelled) return;
        if (!data) throw new Error("The file metadata could not be read.");
        setInfo(data as FileInfo);
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

  const displayName = info?.name || access?.name || "File";
  const mime = effectiveFileMime(
    info?.mime_type || access?.mime_type || "",
    displayName,
  );
  const kind = filePreviewKind(mime);
  const kindLabel = kind.charAt(0).toUpperCase() + kind.slice(1);
  const size = info?.size_bytes ?? access?.size_bytes;
  const creator = readableCreator(info?.created_by);
  const resolved = Boolean(info && vault && !infoLoading && !loadError && !accessChecking);
  usePublishResourceLocation(
    resolved ? { vault: vault!, title: info!.name, kind: "File", collectionPath: info!.collection } : null,
  );

  if (accessChecking || infoLoading) {
    return <FilePageLoading />;
  }

  return (
    <ResourceWorkspace label="File workspace" variant="reading">
      <h1 className="sr-only">{loadError ? "File unavailable" : displayName}</h1>
      <ResourceCommandRow
        meta={resolved ? (
          <>
            <Badge variant="outline">{kindLabel}</Badge>
            {size !== undefined && (
              <span className="whitespace-nowrap tabular-nums">{formatFileSize(size)}</span>
            )}
            {info?.description && (
              <TooltipText tip={info.description} className="hidden min-w-0 truncate xl:block">
                {info.description}
              </TooltipText>
            )}
          </>
        ) : undefined}
      >
        {resolved && (
          <>
            {access?.download_url ? (
              <Button asChild size="sm" className="min-h-11 sm:min-h-0">
                <a
                  href={access.download_url}
                  target="_blank"
                  rel="noreferrer"
                  download={displayName}
                  aria-label="Download file"
                >
                  <Download className="h-4 w-4" aria-hidden />
                  Download
                </a>
              </Button>
            ) : (
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="min-h-11 sm:min-h-0"
                loading={previewLoading}
                onClick={() => void loadPreview()}
              >
                {!previewLoading && <RefreshCw className="h-4 w-4" aria-hidden />}
                Retry preview
              </Button>
            )}
            <Dialog>
              <DialogTrigger asChild>
                <Button type="button" variant="ghost" size="sm" className="min-h-11 sm:min-h-0">
                  <Info className="h-4 w-4" aria-hidden />
                  Info
                </Button>
              </DialogTrigger>
              <DialogContent aria-describedby={undefined}>
                <DialogTitle>File info</DialogTitle>
                <dl className="space-y-3 text-sm">
                  <FileMetadata label="Name" value={displayName} />
                  {info?.description && <FileMetadata label="Description" value={info.description} />}
                  {info?.collection && <FileMetadata label="Collection" value={info.collection} />}
                  {mime && <FileMetadata label="Format" value={mime} />}
                  {size !== undefined && <FileMetadata label="Size" value={formatFileSize(size)} />}
                  {creator && <FileMetadata label="Uploaded by" value={creator} />}
                  {info?.created_at && <FileMetadata label="Created" value={timeAgo(info.created_at)} />}
                </dl>
              </DialogContent>
            </Dialog>
            {canDelete && (
              <ResourceActionsMenu
                resourceName={displayName}
                publishLabel={info?.uri ? "Publish file" : undefined}
                onPublish={info?.uri ? () => setPublishOpen(true) : undefined}
                deleteLabel="Delete file"
                onDelete={() => setDeleteOpen(true)}
              />
            )}
          </>
        )}
      </ResourceCommandRow>

      {published && (
        <PublicationSuccessBanner
          vault={vault!}
          publication={published}
          resourceLabel="File"
          onDismiss={() => setPublished(null)}
        />
      )}

      <div className="relative min-h-0 flex-1 overflow-hidden">
        <ResourceCanvas variant="reading">
          {loadError && !info ? (
            <div className="mx-auto w-full max-w-2xl p-4 sm:p-6">
              <Alert variant="destructive" title="File unavailable">
                {loadError}
              </Alert>
            </div>
          ) : (
            <section aria-label="File preview" className="min-h-0 flex-1 overflow-auto">
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
            </section>
          )}
        </ResourceCanvas>
      </div>
      <ResourceDeleteDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        kind="file"
        name={displayName}
        onConfirm={async () => {
          await deleteVaultFile(vault!, fileId!);
          refetchTree();
          navigate(`/vault/${vault}`);
        }}
      />
      <PublishOptionsDialog
        open={publishOpen}
        onOpenChange={setPublishOpen}
        vault={vault!}
        resourceType="file"
        resourceUri={info?.uri}
        resourceName={displayName}
        onPublished={(_slug, publication) => {
          if (publication) setPublished(publication);
        }}
      />
    </ResourceWorkspace>
  );
}

function FilePageLoading() {
  return (
    <LoadingState
      label="Loading file"
      className="flex h-full min-h-0 flex-col overflow-hidden bg-surface"
    >
      <div className="flex h-full min-h-0 flex-col overflow-hidden">
        <header className="flex min-h-14 shrink-0 items-center gap-3 border-b border-border px-3 sm:h-10 sm:min-h-10 sm:px-4 lg:px-5">
          <Skeleton className="h-6 w-24 rounded-[var(--radius-sm)]" />
          <Skeleton className="hidden h-4 w-24 rounded-[var(--radius-sm)] sm:block" />
          <Skeleton className="ml-auto h-8 w-24 rounded-[var(--radius-md)]" />
        </header>
        <div className="min-h-0 flex-1 p-4 sm:p-6">
          <Skeleton className="h-full min-h-56 w-full rounded-[var(--radius-md)]" />
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

function FileMetadata({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-foreground-muted">{label}</dt>
      <dd className="mt-1 break-words text-foreground">{value}</dd>
    </div>
  );
}
