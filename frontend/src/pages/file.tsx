import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { Download, File } from "lucide-react";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { parseFileUri } from "@/lib/uri";

interface FileInfo {
  uri: string;
  name: string;
  collection?: string;
  description?: string;
  mime_type?: string;
  size_bytes?: number;
  created_by?: string;
  created_at?: string;
  download_url?: string;
}

export default function FilePage() {
  const { name: vault, id: fileId } = useParams<{ name: string; id: string }>();
  const [info, setInfo] = useState<FileInfo | null>(null);
  const [error, setError] = useState("");
  const [downloading, setDownloading] = useState(false);

  useEffect(() => {
    if (!vault || !fileId) return;
    let cancelled = false;
    // Reset stale state from previous param before re-fetch resolves.
    setInfo(null);
    setError("");
    const t = localStorage.getItem("akb_token") || "";
    fetch(`/api/v1/files/${vault}`, { headers: { Authorization: `Bearer ${t}` } })
      .then(async (r) => {
        if (!r.ok) {
          if (!cancelled) setError(`Couldn't load the file list (${r.status}).`);
          return null;
        }
        return r.json().catch(() => null);
      })
      .then((d) => {
        if (cancelled || !d) return;
        // Backend rows carry a canonical `uri` (akb://{vault}/file/{id}) and
        // no separate `id` field after the URI-canonical refactor.
        const found = (d.items || []).find(
          (x: any) => parseFileUri(x.uri)?.id === fileId,
        );
        if (found) setInfo(found);
        else setError("File not found in vault.");
      })
      .catch((e) => !cancelled && setError(String(e)));
    return () => { cancelled = true; };
  }, [vault, fileId]);

  async function download() {
    setDownloading(true);
    setError("");
    try {
      const t = localStorage.getItem("akb_token") || "";
      const r = await fetch(`/api/v1/files/${vault}/${fileId}/download`, {
        headers: { Authorization: `Bearer ${t}` },
      });
      if (!r.ok) {
        setError(`Download failed (${r.status}).`);
        return;
      }
      const d = await r.json().catch(() => ({}));
      if (d.download_url) window.open(d.download_url, "_blank");
      else setError(d.error || d.detail || "Failed to get a download URL.");
    } catch (e) {
      setError(String(e));
    } finally {
      setDownloading(false);
    }
  }

  const loading = info === null && !error;

  return (
    <div className="min-w-0 fade-up max-w-[1280px] mx-auto">
      <div className="coord mb-3">
        VAULT · {vault} · FILE · {info?.name || fileId}
      </div>

      <header className="flex items-baseline justify-between flex-wrap gap-x-4 gap-y-2 pb-3 border-b border-border">
        <h1 className="font-mono text-[28px] font-semibold tracking-tight text-foreground break-all min-w-0">
          {info?.name || fileId}
        </h1>
        <div className="flex items-center gap-4 coord tabular-nums shrink-0">
          {info?.mime_type && (
            <span className="text-foreground-muted font-mono">{info.mime_type}</span>
          )}
          {info?.size_bytes !== undefined && <span>{formatSize(info.size_bytes)}</span>}
          {info?.collection && <span>{info.collection}</span>}
        </div>
      </header>

      {info?.description && (
        <p className="font-medium tracking-[-0.01em] text-[17px] leading-[1.55] text-foreground-muted mt-3">
          {info.description}
        </p>
      )}

      {error && <Alert variant="destructive" className="mt-6">{error}</Alert>}

      {loading && (
        <div className="mt-6 space-y-2" role="status" aria-live="polite" aria-label="Loading file">
          <Skeleton className="h-4 w-64 rounded-[var(--radius-sm)]" />
          <Skeleton className="h-40 w-full rounded-[var(--radius-lg)]" />
        </div>
      )}

      {!loading && (
      <div className="mt-8 rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm p-10 text-center">
        <File className="h-12 w-12 text-foreground-muted mx-auto mb-4" aria-hidden />
        <p className="text-sm font-medium text-foreground">Inline preview pending</p>
        <p className="mt-1 text-sm text-foreground-muted max-w-md mx-auto leading-relaxed">
          Authenticated in-browser previews aren't built yet — fetch the original via presigned S3.
        </p>
        <Button onClick={download} loading={downloading} disabled={!!error} variant="accent" className="mt-5">
          {!downloading && <Download className="h-4 w-4" aria-hidden />}
          {downloading ? "Preparing…" : "Download"}
        </Button>
      </div>
      )}
    </div>
  );
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
}
