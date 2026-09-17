export function filePreviewKind(mime: string): string {
  if (mime.startsWith("image/")) return "image";
  if (mime === "application/pdf") return "pdf";
  if (mime === "application/json") return "json";
  if (mime === "text/html") return "html";
  if (mime.startsWith("text/")) return "text";
  return "binary";
}

// Derive a usable MIME from the filename only when the stored value is
// missing or non-informative. An explicit MIME always wins.
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

export function effectiveFileMime(mime: string, name: string): string {
  if (mime && mime !== "application/octet-stream") return mime;
  const dot = name.lastIndexOf(".");
  if (dot < 0) return mime || "application/octet-stream";
  const ext = name.slice(dot + 1).toLowerCase();
  return EXT_TO_MIME[ext] || mime || "application/octet-stream";
}

export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
}
