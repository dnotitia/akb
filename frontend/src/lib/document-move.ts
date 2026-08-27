/**
 * Browser preview of the backend's Unicode-aware slug normalization.
 * The move response remains authoritative and is always used for navigation.
 */
export function previewDocumentSlug(value: string) {
  const cleaned = value
    .toLocaleLowerCase()
    .trim()
    .replace(/[^\p{L}\p{N}_\s-]/gu, "")
    .replace(/[-\s]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return Array.from(cleaned).slice(0, 80).join("").replace(/-+$/g, "") || "untitled";
}
