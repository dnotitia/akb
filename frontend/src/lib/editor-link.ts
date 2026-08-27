import { sanitizeLinkUrl } from "@/lib/utils";

export function normalizeEditorLinkUrl(raw: string): string | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const withScheme = /^[\w.-]+\.[a-z]{2,}(?:[/:?#]|$)/i.test(trimmed)
    ? `https://${trimmed}`
    : trimmed;
  const safe = sanitizeLinkUrl(withScheme);
  return safe === "#" && withScheme !== "#" ? null : safe;
}
