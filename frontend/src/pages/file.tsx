import { useCallback, useEffect, useRef, useState } from "react";
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
import { InlineLoadingState, LoadingState } from "@/components/ui/loading-state";
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
  expires_in?: number;
}

class FileRequestError extends Error {
  constructor(public readonly status: number, message: string) { super(message); }
}

function fileRequestError(status: number, action: string): FileRequestError {
  return new FileRequestError(status, status === 403
    ? "You no longer have permission to read this file."
    : status === 404 ? "File not found in vault."
      : `Couldn't ${action} (${status}). Try again.`);
}

export default function FilePage() {
  const { name: vault, id: fileId } = useParams<{ name: string; id: string }>();
  const user = useCurrentUser();
  const { checking, revision } = useAccessVerification();
  return <FilePageContent key={JSON.stringify([user?.user_id, revision, vault, fileId, checking])} accessChecking={checking} />;
}

function FilePageContent({ accessChecking }: { accessChecking: boolean }) {
  const { name: vault, id: fileId } = useParams<{ name: string; id: string }>();
  const navigate = useNavigate();
  const { refetchTree } = useVaultRefresh();
  const active = useRef(true);
  const metadataGeneration = useRef(0);
  const permissionGeneration = useRef(0);
  const previewGeneration = useRef(0);
  const previewController = useRef<AbortController | null>(null);
  const [info, setInfo] = useState<FileInfo | null>(null);
  const [access, setAccess] = useState<FileAccess | null>(null);
  // A renewed download grant must not reset the currently mounted reader.
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [loadError, setLoadError] = useState("");
  const [previewError, setPreviewError] = useState("");
  const [infoLoading, setInfoLoading] = useState(true);
  const [previewLoading, setPreviewLoading] = useState(true);
  const [canDelete, setCanDelete] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [publishOpen, setPublishOpen] = useState(false);
  const [published, setPublished] = useState<Publication | null>(null);
  const [previewVersion, setPreviewVersion] = useState(0);
  const [expiresAt, setExpiresAt] = useState<number | null>(null);
  const [linkExpired, setLinkExpired] = useState(false);

  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
      metadataGeneration.current += 1;
      previewGeneration.current += 1;
      previewController.current?.abort();
    };
  }, []);

  const loadPermissions = useCallback(async () => {
    if (!vault || accessChecking || !active.current) return;
    const generation = ++permissionGeneration.current;
    setCanDelete(false);
    try {
      const data = await getVaultInfo(vault);
      if (!active.current || generation !== permissionGeneration.current) return;
      setCanDelete(
        canEdit(data?.role) && !data?.is_archived && !data?.is_external_git &&
          (!data?.managed_by || data?.role_source === "system_admin"),
      );
    } catch {
      if (active.current && generation === permissionGeneration.current) setCanDelete(false);
    }
  }, [vault, accessChecking]);

  useEffect(() => { void loadPermissions(); }, [loadPermissions]);

  const loadInfo = useCallback(async () => {
    if (!vault || !fileId || accessChecking || !active.current) return;
    const generation = ++metadataGeneration.current;
    setInfo(null);
    setLoadError("");
    setInfoLoading(true);

    // Resolve the File by id. This used to read the vault listing and search
    // the page that came back, so any File outside that window reported itself
    // as missing however reachable it actually was.
    try {
      const response = await authenticatedFetch(
        `/api/v1/files/${encodeURIComponent(vault)}/${encodeURIComponent(fileId)}`,
      );
      if (!response.ok) throw fileRequestError(response.status, "load the file");
      const data = await response.json().catch(() => null);
      if (!active.current || generation !== metadataGeneration.current) return;
      if (!data) throw new Error("The file metadata could not be read.");
      setInfo(data as FileInfo);
    } catch (error) {
      if (!active.current || generation !== metadataGeneration.current) return;
      setLoadError(error instanceof Error ? error.message : String(error));
      setAccess(null);
      setPreviewUrl(null);
      setCanDelete(false);
      permissionGeneration.current += 1;
    } finally {
      if (active.current && generation === metadataGeneration.current) setInfoLoading(false);
    }
  }, [vault, fileId, accessChecking]);

  useEffect(() => { void loadInfo(); }, [loadInfo]);

  const loadPreview = useCallback(async (restartViewer = false) => {
    if (!vault || !fileId || accessChecking || !active.current) return;
    const generation = ++previewGeneration.current;
    previewController.current?.abort();
    const controller = new AbortController();
    previewController.current = controller;
    setPreviewLoading(true);
    setPreviewError("");
    try {
      const response = await authenticatedFetch(
        `/api/v1/files/${encodeURIComponent(vault)}/${encodeURIComponent(fileId)}/download`,
        { signal: controller.signal },
      );
      if (!response.ok) throw fileRequestError(response.status, "refresh the file link");
      const data = await response.json().catch(() => ({}));
      if (!active.current || generation !== previewGeneration.current) return;
      if (typeof data.download_url !== "string" || !data.download_url) {
        throw new Error("The original file URL was not returned.");
      }
      setAccess(data);
      setPreviewUrl(current => restartViewer || !current ? data.download_url : current);
      setExpiresAt(typeof data.expires_in === "number" && Number.isFinite(data.expires_in)
        ? Date.now() + Math.max(0, data.expires_in) * 1000 : null);
      setLinkExpired(false);
      if (restartViewer) setPreviewVersion((version) => version + 1);
    } catch (error) {
      if (!active.current || generation !== previewGeneration.current) return;
      setPreviewError(error instanceof Error ? error.message : String(error));
      if (error instanceof FileRequestError && [401, 403, 404].includes(error.status)) {
        setAccess(null);
        setPreviewUrl(null);
        setInfo(null);
        setCanDelete(false);
        permissionGeneration.current += 1;
        setLoadError(error.message);
        metadataGeneration.current += 1;
        setInfoLoading(false);
      }
    } finally {
      if (active.current && generation === previewGeneration.current) setPreviewLoading(false);
    }
  }, [vault, fileId, accessChecking]);

  useEffect(() => {
    void loadPreview();
  }, [loadPreview]);

  useEffect(() => {
    if (expiresAt === null) return;
    let timer: ReturnType<typeof setTimeout>;
    const check = () => {
      const remaining = expiresAt - Date.now();
      if (remaining <= 0) setLinkExpired(true);
      else timer = setTimeout(check, Math.min(remaining, 2_147_483_647));
    };
    check();
    return () => clearTimeout(timer);
  }, [expiresAt]);

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
    resolved ? { vault: vault!, title: info!.name, kind: "File", collectionPath: info!.collection, resourceRef: fileId } : null,
  );

  if (accessChecking || infoLoading) {
    return <FilePageLoading />;
  }

  return (
    <ResourceWorkspace label="File workspace" variant="reading">
      <h1 className="sr-only">{loadError ? "File unavailable" : displayName}</h1>
      <ResourceCommandRow
        meta={resolved ? (
          <div className="hidden min-w-0 items-center gap-3 sm:flex">
            <Badge variant="outline">{kindLabel}</Badge>
            {size !== undefined && (
              <span className="whitespace-nowrap tabular-nums">{formatFileSize(size)}</span>
            )}
            {info?.description && (
              <TooltipText tip={info.description} className="hidden min-w-0 truncate xl:block">
                {info.description}
              </TooltipText>
            )}
          </div>
        ) : undefined}
      >
        {resolved && (
          <>
            {access?.download_url && !linkExpired ? (
              <Button asChild size="sm" className="min-h-11 sm:min-h-0">
                <a
                  href={access.download_url}
                  target="_blank"
                  rel="noreferrer"
                  download={displayName}
                  aria-label="Download file"
                  onClick={(event) => {
                    if (expiresAt !== null && Date.now() >= expiresAt) {
                      event.preventDefault();
                      void loadPreview();
                    }
                  }}
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
                disabled
                aria-label="Download file"
              >
                <Download className="h-4 w-4" aria-hidden />
                Download
              </Button>
            )}
            <Button variant="ghost" size="sm" loading={previewLoading} onClick={() => void loadPreview()}>
              {!previewLoading && <RefreshCw className="h-4 w-4" aria-hidden />}
              Refresh link
            </Button>
            {previewUrl && access?.download_url && previewUrl !== access.download_url && !linkExpired && (
              <Button variant="ghost" size="sm" disabled={previewLoading} onClick={() => { setPreviewUrl(access.download_url!); setPreviewVersion(version => version + 1); }}>Reload preview</Button>
            )}
            <Dialog>
              <DialogTrigger asChild>
                <Button type="button" variant="ghost" size="sm" className="min-h-11 sm:min-h-0">
                  <Info className="h-4 w-4" aria-hidden />
                  Info
                </Button>
              </DialogTrigger>
              <DialogContent aria-describedby={undefined} className="left-auto right-0 top-0 flex h-dvh max-h-dvh w-full max-w-sm translate-x-0 translate-y-0 flex-col gap-0 overflow-hidden rounded-none p-0">
                <DialogTitle className="shrink-0 border-b border-border px-5 py-4 pr-14">File info</DialogTitle>
                <dl className="min-h-0 space-y-4 overflow-y-auto p-5 text-sm">
                  <FileMetadata label="Name" value={displayName} />
                  {info?.description && <FileMetadata label="Description" value={info.description} />}
                  {info?.collection && <FileMetadata label="Collection" value={info.collection} />}
                  {mime && <FileMetadata label="Format" value={mime} />}
                  {size !== undefined && <FileMetadata label="Size" value={formatFileSize(size)} />}
                  {creator && <FileMetadata label="Uploaded by" value={creator} />}
                  {info?.created_at && Number.isFinite(Date.parse(info.created_at)) && (
                    <FileMetadata label="Created" value={`${new Date(info.created_at).toLocaleString()} (${timeAgo(info.created_at)})`} />
                  )}
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
                <div className="mt-3">
                  <Button variant="outline" size="sm" onClick={() => { void loadInfo(); void loadPreview(); void loadPermissions(); }}>
                    Try again
                  </Button>
                </div>
              </Alert>
            </div>
          ) : (
            <section aria-label="File preview" aria-busy={previewLoading} className="min-h-0 flex-1 overflow-auto">
                {previewLoading && !access?.download_url ? (
                  <FilePreviewLoading />
                ) : !access?.download_url ? (
                  <div className="mx-auto flex min-h-64 max-w-xl items-center px-4 py-10">
                    <Alert variant="warning" title="Preview unavailable" className="w-full">
                      <div className="space-y-3">
                        <p>{linkExpired ? "The file link has expired. Refresh it to preview or download again." : previewError || "The original file URL was not returned."}</p>
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          onClick={() => void loadPreview()}
                        >
                          <RefreshCw className="h-4 w-4" aria-hidden />
                          Refresh link
                        </Button>
                      </div>
                    </Alert>
                  </div>
                ) : (
                  <>
                  {previewLoading && <InlineLoadingState label="Refreshing file link; current preview remains visible" className="px-4 py-2" />}
                  {(previewError || linkExpired) && <Alert variant="warning" className="m-4" title="File link needs attention">
                    <p>{linkExpired ? "The file link has expired. Refresh it to preview or download again." : previewError}</p>
                    <p className="mt-1 text-xs">The current preview is retained. Use Refresh link above to try again.</p>
                  </Alert>}
                  <FilePreviewBody
                    key={`${previewUrl}:${previewVersion}`}
                    mime={mime}
                    directUrl={previewUrl ?? access.download_url}
                    rawUrl={previewUrl ?? access.download_url}
                    name={displayName}
                    sizeBytes={size}
                    onRefreshLink={() => void loadPreview(true)}
                  />
                  </>
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
          if (!active.current || !canDelete) throw new Error("File deletion is not available with your current permissions.");
          await deleteVaultFile(vault!, fileId!);
          if (!active.current) return;
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
          if (!active.current) return;
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
