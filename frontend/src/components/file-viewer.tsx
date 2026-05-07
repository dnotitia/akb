import { useCallback, useEffect, useRef, useState } from "react";
import { JsonTree } from "@/components/json-tree";
import {
  publicationDownloadUrl,
  publicationRawUrl,
  type PublicationResponse,
} from "@/lib/api";

interface Props {
  slug: string;
  data: PublicationResponse;
}

export function FileViewer({ slug, data }: Props) {
  // Defensive fallback: if the DB mime_type is missing or the generic
  // application/octet-stream (legacy uploads from proxy <0.5.1), derive one
  // from the filename extension so preview still works.
  const rawMime = data.mime_type || "";
  const mime = effectiveMime(rawMime, data.name || "");
  const downloadUrl = publicationDownloadUrl(slug);
  const rawUrl = publicationRawUrl(slug);
  const kind = pickKind(mime);

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[180px_1fr] gap-8">
      {/* Left rail */}
      <aside className="lg:sticky lg:top-8 lg:self-start space-y-5">
        <div>
          <div className="coord mb-1">TYPE</div>
          <div className="text-sm font-medium">file</div>
        </div>
        <div>
          <div className="coord mb-1">FORMAT</div>
          <div className="text-sm font-medium font-mono break-all">
            {kind.toUpperCase()}
          </div>
          <div className="coord mt-1 break-all">{mime || "—"}</div>
        </div>
        {data.size_bytes !== undefined && (
          <div>
            <div className="coord mb-1">SIZE</div>
            <div className="font-display-tight text-2xl text-foreground">
              {formatSize(data.size_bytes)}
            </div>
          </div>
        )}
        {data.collection && (
          <div>
            <div className="coord mb-1">COLLECTION</div>
            <div className="text-sm font-medium font-mono break-all">{data.collection}</div>
          </div>
        )}
        <div className="pt-3 border-t border-border">
          <a
            href={downloadUrl}
            download={data.name}
            className="block coord-spark hover:underline"
          >
            ↓ DOWNLOAD ORIGINAL
          </a>
        </div>
      </aside>

      {/* Main column */}
      <div className="min-w-0">
        <div className="coord-spark mb-4">§ FILE · {kind.toUpperCase()}</div>
        <h1 className="font-display-tight text-5xl lg:text-6xl text-foreground leading-[0.95] tracking-tight mb-2 break-words">
          {data.title || data.name}
        </h1>
        {data.title && data.name && data.title !== data.name && (
          <div className="coord mb-8">{data.name}</div>
        )}

        <div className="border border-border mt-8">
          <div className="border-b border-border bg-foreground text-background px-3 py-1.5 flex items-center justify-between">
            <span className="coord-ink" style={{ color: "var(--color-paper)" }}>
              ⊞ PREVIEW
            </span>
            <span className="coord-ink" style={{ color: "var(--color-paper)" }}>
              {kind.toUpperCase()}
            </span>
          </div>
          <FileBody
            mime={mime}
            directUrl={data.download_url || ""}
            rawUrl={rawUrl}
            name={data.name || ""}
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
}

function FileBody({ mime, directUrl, rawUrl, name }: FileBodyProps) {
  if (!directUrl) {
    return (
      <div className="p-8 text-center">
        <div className="coord">— NO CONTENT AVAILABLE —</div>
      </div>
    );
  }

  if (mime.startsWith("image/")) {
    return (
      <div className="flex justify-center bg-whisper p-6">
        <img src={directUrl} alt={name} className="max-w-full max-h-[80vh]" />
      </div>
    );
  }

  if (mime === "application/pdf") {
    return (
      <embed
        src={directUrl}
        type="application/pdf"
        width="100%"
        height="800"
        className="w-full"
      />
    );
  }

  if (mime === "text/html") {
    return <HtmlFileBody rawUrl={rawUrl} name={name} />;
  }

  if (mime === "application/json") {
    return <JsonFileBody url={rawUrl} />;
  }

  if (mime.startsWith("text/")) {
    return <TextFileBody url={rawUrl} />;
  }

  return (
    <div className="p-12 text-center">
      <div className="coord mb-2">— PREVIEW UNAVAILABLE —</div>
      <p className="text-sm text-foreground-muted">
        No inline view for <code className="font-mono">{mime || "this format"}</code>.
        Use the download link in the side rail.
      </p>
    </div>
  );
}

function HtmlFileBody({ rawUrl, name }: { rawUrl: string; name: string }) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
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
    <iframe
      ref={iframeRef}
      src={rawUrl}
      sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox allow-top-navigation-by-user-activation"
      className="w-full h-[80vh]"
      title={name}
      onLoad={onLoad}
    />
  );
}

function JsonFileBody({ url }: { url: string }) {
  const [json, setJson] = useState<any>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    fetch(url)
      .then((r) => r.json())
      .then(setJson)
      .catch((e) => setError(String(e)));
  }, [url]);
  if (error) return <div className="p-4 coord-spark" style={{ color: "var(--color-destructive)" }}>⚠ {error}</div>;
  if (json === null) return <div className="p-4 coord">— LOADING —</div>;
  return (
    <div className="font-mono text-sm overflow-auto p-4 max-h-[80vh]">
      <JsonTree data={json} />
    </div>
  );
}

function TextFileBody({ url }: { url: string }) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    fetch(url)
      .then((r) => r.text())
      .then(setText)
      .catch((e) => setError(String(e)));
  }, [url]);
  if (error) return <div className="p-4 coord-spark" style={{ color: "var(--color-destructive)" }}>⚠ {error}</div>;
  if (text === null) return <div className="p-4 coord">— LOADING —</div>;
  return (
    <pre className="text-sm whitespace-pre-wrap font-mono p-4 overflow-auto max-h-[80vh] bg-surface">
      {text}
    </pre>
  );
}

function pickKind(mime: string): string {
  if (mime.startsWith("image/")) return "image";
  if (mime === "application/pdf") return "pdf";
  if (mime === "application/json") return "json";
  if (mime === "text/html") return "html";
  if (mime.startsWith("text/")) return "text";
  return "binary";
}

// Derive a usable MIME from filename extension when the stored mime_type is
// missing or the generic application/octet-stream. Only overrides when the
// stored value is non-informative — an explicit mime wins.
const EXT_TO_MIME: Record<string, string> = {
  html: "text/html", htm: "text/html",
  pdf: "application/pdf",
  json: "application/json", xml: "application/xml",
  txt: "text/plain", md: "text/markdown", log: "text/plain",
  csv: "text/csv", tsv: "text/tab-separated-values",
  css: "text/css", js: "text/javascript", mjs: "text/javascript",
  yaml: "application/yaml", yml: "application/yaml",
  png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg",
  gif: "image/gif", webp: "image/webp", svg: "image/svg+xml",
  bmp: "image/bmp", ico: "image/x-icon",
  mp3: "audio/mpeg", wav: "audio/wav",
  mp4: "video/mp4", webm: "video/webm",
};

function effectiveMime(mime: string, name: string): string {
  if (mime && mime !== "application/octet-stream") return mime;
  const dot = name.lastIndexOf(".");
  if (dot < 0) return mime || "application/octet-stream";
  const ext = name.slice(dot + 1).toLowerCase();
  return EXT_TO_MIME[ext] || mime || "application/octet-stream";
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
}
