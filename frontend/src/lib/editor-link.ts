import { sanitizeLinkUrl } from "@/lib/utils";
import { parseUri } from "@/lib/uri";
import { canonicalAkbMarkdownTarget } from "@/lib/markdown-adapters";

export function isAkbResourceTarget(raw: string | null | undefined): boolean {
  const parsed = parseUri(raw?.trim() || "");
  return parsed?.kind === "doc" || parsed?.kind === "file";
}

export function normalizeEditorLinkUrl(raw: string): string | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  if (isAkbResourceTarget(trimmed)) return canonicalAkbMarkdownTarget(trimmed) ?? trimmed;
  const withScheme = /^[\w.-]+\.[a-z]{2,}(?:[/:?#]|$)/i.test(trimmed)
    ? `https://${trimmed}`
    : trimmed;
  const safe = sanitizeLinkUrl(withScheme);
  return safe === "#" && withScheme !== "#" ? null : safe;
}
