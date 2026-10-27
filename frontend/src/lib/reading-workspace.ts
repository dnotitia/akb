export interface ReadingDocument {
  vault: string;
  path: string;
  title: string;
  version?: string;
  href: string;
}

export function readingDocumentKey(doc: Pick<ReadingDocument, "vault" | "path" | "version">) {
  return JSON.stringify([doc.vault, doc.path, doc.version ?? ""]);
}

/** Only same-origin document routes participate. Everything else keeps its
 * native link behavior, including downloads, files, tables and external URLs. */
export function readingReference(href: string, origin: string): ReadingDocument | null {
  try {
    const url = new URL(href, origin);
    if (url.origin !== new URL(origin).origin) return null;
    const match = url.pathname.match(/^\/vault\/([^/]+)\/doc\/(.+)$/);
    if (!match || ["edit", "diff"].includes(url.searchParams.get("view") ?? "")) return null;
    const vault = decodeURIComponent(match[1]);
    const path = decodeURIComponent(match[2]);
    return { vault, path, title: path.split("/").at(-1) || path,
      version: url.searchParams.get("commit") || undefined,
      href: url.pathname + url.search + url.hash };
  } catch { return null; }
}

export function readingDocumentHref(vault: string, path: string, search = "") {
  return `/vault/${encodeURIComponent(vault)}/doc/${encodeURIComponent(path)}${search}`;
}

export function readingFragment(hash: string) {
  try { return decodeURIComponent(hash.replace(/^#/, "")); }
  catch { return ""; }
}
