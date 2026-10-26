import { useCallback, useEffect, useRef, useState } from "react";
import { Download } from "lucide-react";
import { JsonTree } from "@/components/json-tree";
import { Alert } from "@/components/ui/alert";
import { CopyButton } from "@/components/ui/copy-button";
import { LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { FilePreviewLimitError, readBoundedFilePreview } from "@/lib/bounded-file-preview";
import {
  publicationDownloadUrl,
  publicationRawUrl,
  type PublicationResponse,
} from "@/lib/api";
import {
  effectiveFileMime,
  filePreviewKind,
  formatFileSize,
} from "@/lib/file-preview";

interface Props {
  slug: string;
  data: PublicationResponse;
}

export function FileViewer({ slug, data }: Props) {
  // Defensive fallback: if the DB mime_type is missing or the generic
  // application/octet-stream (legacy uploads from proxy <0.5.1), derive one
  // from the filename extension so preview still works.
  const rawMime = data.mime_type || "";
  const mime = effectiveFileMime(rawMime, data.name || "");
  const downloadUrl = publicationDownloadUrl(slug);
  const rawUrl = publicationRawUrl(slug);
  const kind = filePreviewKind(mime);
  const kindLabel = kind.charAt(0).toUpperCase() + kind.slice(1);

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[180px_1fr] gap-8">
      {/* Left rail */}
      <aside className="lg:sticky lg:top-8 lg:self-start space-y-5">
        <div>
          <div className="coord mb-1">Type</div>
          <div className="text-sm font-medium">file</div>
        </div>
        <div>
          <div className="coord mb-1">Format</div>
          <div className="text-sm font-medium font-mono break-all">
            {kindLabel}
          </div>
          <div className="coord mt-1 break-all">{mime || "—"}</div>
        </div>
        {data.size_bytes !== undefined && (
          <div>
            <div className="coord mb-1">Size</div>
            <div className="font-display text-2xl font-semibold tracking-tight text-foreground">
              {formatFileSize(data.size_bytes)}
            </div>
          </div>
        )}
        {data.collection && (
          <div>
            <div className="coord mb-1">Collection</div>
            <div className="inline-flex items-center gap-1.5">
              <div className="text-sm font-medium font-mono break-all">{data.collection}</div>
              <CopyButton value={data.collection} label="Copy collection" />
            </div>
          </div>
        )}
        <div className="pt-3 border-t border-border">
          <a
            href={downloadUrl}
            download={data.name}
            className="inline-flex items-center gap-1.5 rounded-[var(--radius-sm)] text-sm font-medium text-link underline underline-offset-4 transition-token hover:text-link-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            <Download className="h-4 w-4" aria-hidden />
            Download original
          </a>
        </div>
      </aside>

      {/* Main column */}
      <div className="min-w-0">
        <div className="coord mb-4">File · {kindLabel}</div>
        <h1 className="font-display text-3xl font-semibold tracking-tight text-foreground mb-2 break-words">
          {data.title || data.name}
        </h1>
        {data.title && data.name && data.title !== data.name && (
          <div className="coord mb-8">{data.name}</div>
        )}

        <div className="rounded-[var(--radius-lg)] border border-border overflow-hidden shadow-sm mt-8">
          <div className="border-b border-border bg-surface-2 text-foreground px-3 py-1.5 flex items-center justify-between">
            <span className="coord-ink">
              ⊞ Preview
            </span>
            <span className="coord-ink">
              {kindLabel}
            </span>
          </div>
          <FilePreviewBody
            mime={mime}
            // Images/PDFs render inline from the same-origin proxy (not a
            // presigned S3 URL) so the vault name never leaks and the view stays
            // counted + revocable. (publish-hardening F4.)
            directUrl={rawUrl}
            rawUrl={rawUrl}
            name={data.name || ""}
            sizeBytes={data.size_bytes}
          />
        </div>
      </div>
    </div>
  );
}

interface FileBodyProps {
  mime: string;
  directUrl: string;
  rawUrl: string;
  name: string;
  sizeBytes?: number;
  onRefreshLink?: () => void;
}

export function FilePreviewBody({ mime, directUrl, rawUrl, name, sizeBytes, onRefreshLink }: FileBodyProps) {
  if (!directUrl) {
    return (
      <div className="p-8 text-center">
        <div className="coord">— No content available —</div>
      </div>
    );
  }

  if (mime.startsWith("image/")) {
    return <ImageFileBody key={directUrl} directUrl={directUrl} name={name} onRefreshLink={onRefreshLink} />;
  }

  if (mime === "application/pdf") {
    return <PdfFileBody key={directUrl} directUrl={directUrl} name={name} onRefreshLink={onRefreshLink} />;
  }

  if (mime === "text/html") {
    return <HtmlFileBody key={rawUrl} rawUrl={rawUrl} name={name} />;
  }

  if (mime === "application/json") {
    return <JsonFileBody key={rawUrl} url={rawUrl} onRefreshLink={onRefreshLink} />;
  }

  if (filePreviewKind(mime) === "text") {
    return <TextFileBody key={rawUrl} url={rawUrl} onRefreshLink={onRefreshLink} />;
  }

  return (
    <div className="mx-auto max-w-xl break-words p-6 text-center sm:p-12">
      {name && <p className="mb-3 font-semibold text-foreground">{name}</p>}
      <p className="mb-2 text-sm font-medium text-foreground">Preview unavailable</p>
      {sizeBytes !== undefined && <p className="mb-2 text-sm text-foreground-muted">{formatFileSize(sizeBytes)}</p>}
      <p className="text-sm text-foreground-muted">
        No inline view for <code className="font-mono">{mime || "this format"}</code>.
        Download the original file to open it locally.
      </p>
    </div>
  );
}

function ImageFileBody({ directUrl, name, onRefreshLink }: { directUrl: string; name: string; onRefreshLink?: () => void }) {
  const [failed, setFailed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [actualSize, setActualSize] = useState(false);
  if (failed) {
    return (
      <PreviewFailure
        error={new Error("The image couldn't be loaded. Refresh the link to check current access, or download the original file.")}
        onRetry={onRefreshLink || (() => { setFailed(false); setLoading(true); })}
        refreshLink={Boolean(onRefreshLink)}
      />
    );
  }
  return (
    <div className="bg-surface-2">
      <div role="group" aria-label="Image preview size" className="flex flex-wrap gap-1 border-b border-border p-2">
        <Button variant="ghost" size="sm" aria-pressed={!actualSize} onClick={() => setActualSize(false)}>Fit image</Button>
        <Button variant="ghost" size="sm" aria-pressed={actualSize} onClick={() => setActualSize(true)}>Actual size</Button>
      </div>
      <div role="region" aria-label="Image file content" tabIndex={0} className="relative min-h-48 max-h-[80vh] overflow-auto p-4 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring sm:p-6">
        {loading && (
          <LoadingState label="Loading image preview" className="absolute inset-0 p-6">
            <Skeleton className="h-full min-h-40 w-full rounded-[var(--radius-md)]" />
          </LoadingState>
        )}
        <img
          src={directUrl}
          alt={name || "File preview image"}
          loading="lazy"
          decoding="async"
          onLoad={() => setLoading(false)}
          onError={() => {
            setLoading(false);
            setFailed(true);
          }}
          className={`${actualSize ? "max-w-none" : "mx-auto max-h-[72vh] max-w-full object-contain"} ${loading ? "opacity-0" : ""}`}
        />
      </div>
    </div>
  );
}

function PdfFileBody({ directUrl, name, onRefreshLink }: { directUrl: string; name: string; onRefreshLink?: () => void }) {
  const [loading, setLoading] = useState(true);
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    if (!loading) return;
    const timer = setTimeout(() => setSlow(true), 10_000);
    return () => clearTimeout(timer);
  }, [loading]);
  return (
    <div className="relative min-h-[80vh]">
      {slow && loading && <div role="status" className="relative z-[var(--z-raised)] border-b border-border bg-surface p-4 text-sm">
        <p>The PDF is taking longer to load. You can refresh its link or open the file directly.</p>
        <div className="mt-2 flex gap-2">
          {onRefreshLink && <Button variant="outline" size="sm" onClick={onRefreshLink}>Refresh link</Button>}
          <Button variant="outline" size="sm" asChild><a href={directUrl} target="_blank" rel="noopener noreferrer">Open PDF</a></Button>
        </div>
      </div>}
      {loading && (
        <LoadingState label="Loading PDF preview" className="absolute inset-0 p-4">
          <Skeleton className="h-full w-full rounded-[var(--radius-md)]" />
        </LoadingState>
      )}
      <iframe
        src={directUrl}
        title={name || "PDF preview"}
        className="h-[80vh] w-full"
        onLoad={() => setLoading(false)}
      />
    </div>
  );
}

function HtmlFileBody({ rawUrl, name }: { rawUrl: string; name: string }) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const [loading, setLoading] = useState(true);
  // contentWidth is measured once per document load; resize just re-scales.
  const contentWidthRef = useRef<number | null>(null);
  const lastWidthRef = useRef<number>(0);
  const rafRef = useRef<number>(0);

  const getOrCreateStyleEl = (doc: Document): HTMLStyleElement => {
    let el = doc.getElementById("akb-fit-style") as HTMLStyleElement | null;
    if (!el) {
      el = doc.createElement("style");
      el.id = "akb-fit-style";
      doc.head?.appendChild(el);
    }
    return el;
  };

  const writeStyle = (styleEl: HTMLStyleElement, contentWidth: number, scale: number) => {
    styleEl.textContent = `
      html { overflow-x: hidden; }
      body { width: ${Math.ceil(contentWidth)}px !important; zoom: ${scale}; }
    `;
  };

  // Inject a style tag into the iframe's document that sizes body to its
  // natural content extent and zooms it down to fit the iframe. Same-origin
  // access works because /public/{slug}/raw proxies through our own origin.
  const applyFit = useCallback(() => {
    if (rafRef.current) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = 0;
      const iframe = iframeRef.current;
      if (!iframe) return;
      const doc = iframe.contentDocument;
      if (!doc || !doc.body) return;
      const containerWidth = iframe.clientWidth;
      if (containerWidth === 0 || containerWidth === lastWidthRef.current) return;
      lastWidthRef.current = containerWidth;

      const styleEl = getOrCreateStyleEl(doc);

      // Measure natural content extent once, then cache — re-measuring would
      // see our previously-written zoom style and report scaled coords.
      if (contentWidthRef.current === null) {
        styleEl.textContent = "";
        let minLeft = 0;
        let maxRight = 0;
        const walk = (el: Element) => {
          const r = el.getBoundingClientRect();
          if (r.left < minLeft) minLeft = r.left;
          if (r.right > maxRight) maxRight = r.right;
          for (const child of el.children) walk(child);
        };
        walk(doc.body);
        contentWidthRef.current = maxRight - minLeft;
      }

      const contentWidth = contentWidthRef.current;
      if (contentWidth <= containerWidth + 1) {
        styleEl.textContent = "";
        return;
      }
      writeStyle(styleEl, contentWidth, containerWidth / contentWidth);
    });
  }, []);

  // Invalidate the cached width when a new document loads (e.g. src change).
  const onLoad = useCallback(() => {
    setLoading(false);
    contentWidthRef.current = null;
    lastWidthRef.current = 0;
    applyFit();
  }, [applyFit]);

  useEffect(() => {
    const iframe = iframeRef.current;
    if (!iframe) return;
    const ro = new ResizeObserver(() => applyFit());
    ro.observe(iframe);
    return () => {
      ro.disconnect();
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
    };
  }, [applyFit]);

  return (
    <div className="relative min-h-[80vh]">
      {loading && (
        <LoadingState label="Loading HTML preview" className="absolute inset-0 p-4">
          <Skeleton className="h-full w-full rounded-[var(--radius-md)]" />
        </LoadingState>
      )}
      <iframe
      ref={iframeRef}
      src={rawUrl}
      // Untrusted user-uploaded HTML. No allow-scripts (content stays inert) and
      // no allow-top-navigation / allow-popups-to-escape-sandbox (those let the
      // page hijack the top frame). allow-same-origin is kept only so the parent
      // can read contentDocument for the fit-to-width scaling below.
      // NOTE (follow-up): allow-same-origin on same-origin user content is still
      // a soft risk — the durable fix is serving /raw from a separate sandboxed
      // origin or with CSP sandbox, backend-side.
      sandbox="allow-same-origin"
        className="h-[80vh] w-full"
        title={name || "HTML file preview"}
        onLoad={onLoad}
      />
    </div>
  );
}

function useBoundedPreview(url: string, json: boolean) {
  const [result, setResult] = useState<{ text: string; truncated: boolean; value?: unknown } | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    setResult(null);
    setError(null);
    fetch(url, { signal: controller.signal })
      .then((response) => readBoundedFilePreview(response, { json, signal: controller.signal }))
      .then((preview) => {
        if (cancelled) return;
        let value: unknown;
        if (json) {
          try { value = JSON.parse(preview.text); }
          catch { throw new Error("This file isn't valid JSON. Download the original file to inspect it."); }
        }
        setResult({ ...preview, value });
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(caught instanceof Error ? caught : new Error(String(caught)));
      });
    return () => { cancelled = true; controller.abort(); };
  }, [url, json, attempt]);
  return { result, error, retry: () => setAttempt((value) => value + 1) };
}

function PreviewFailure({ error, onRetry, refreshLink }: { error: Error; onRetry: () => void; refreshLink: boolean }) {
  return (
    <Alert variant={error instanceof FilePreviewLimitError ? "info" : "destructive"} title="Preview unavailable" className="m-4">
      <p>{error.message}</p>
      {!(error instanceof FilePreviewLimitError) && (
        <Button variant="outline" size="sm" className="mt-3" onClick={onRetry}>
          {refreshLink ? "Refresh link" : "Retry preview"}
        </Button>
      )}
    </Alert>
  );
}

function JsonFileBody({ url, onRefreshLink }: { url: string; onRefreshLink?: () => void }) {
  const { result, error, retry } = useBoundedPreview(url, true);
  if (error) return <PreviewFailure error={error} onRetry={onRefreshLink || retry} refreshLink={Boolean(onRefreshLink)} />;
  if (!result) return <FileBodyLoading label="Loading JSON preview" />;
  return (
    <div role="region" aria-label="JSON file content" tabIndex={0} className="max-h-[80vh] overflow-auto p-4 font-mono text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring">
      <JsonTree data={result.value} />
    </div>
  );
}

function TextFileBody({ url, onRefreshLink }: { url: string; onRefreshLink?: () => void }) {
  const { result, error, retry } = useBoundedPreview(url, false);
  if (error) return <PreviewFailure error={error} onRetry={onRefreshLink || retry} refreshLink={Boolean(onRefreshLink)} />;
  if (!result) return <FileBodyLoading label="Loading text preview" />;
  return (
    <>
      {result.truncated && (
        <Alert variant="info" className="m-4 mb-0">Preview truncated at 512 KB. Download the file for the full content.</Alert>
      )}
      <pre role="region" aria-label="Text file content" tabIndex={0} className="max-h-[80vh] overflow-auto whitespace-pre-wrap bg-surface p-4 font-mono text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring">
        {result.text}
      </pre>
    </>
  );
}

function FileBodyLoading({ label }: { label: string }) {
  return (
    <LoadingState label={label} className="min-h-52 bg-surface p-4">
      <div className="space-y-3">
        <Skeleton className="h-4 w-2/5 rounded-[var(--radius-sm)]" />
        <Skeleton className="h-4 w-full rounded-[var(--radius-sm)]" />
        <Skeleton className="h-4 w-11/12 rounded-[var(--radius-sm)]" />
        <Skeleton className="h-4 w-4/5 rounded-[var(--radius-sm)]" />
        <Skeleton className="h-4 w-5/6 rounded-[var(--radius-sm)]" />
      </div>
    </LoadingState>
  );
}
