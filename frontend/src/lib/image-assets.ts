const ASSET_URL = /^\/api\/assets\/([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\/?$/;

export const EDITOR_IMAGE_MAX_BYTES = 10 * 1024 * 1024;
export const EDITOR_IMAGE_MIME_TYPES = [
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
] as const;

export function assetIdFromUrl(src: string | null | undefined): string | null {
  if (!src) return null;
  return ASSET_URL.exec(src.trim())?.[1] ?? null;
}

export function validateEditorImage(file: File): string | null {
  if (!EDITOR_IMAGE_MIME_TYPES.includes(file.type as (typeof EDITOR_IMAGE_MIME_TYPES)[number])) {
    return "Choose a PNG, JPEG, GIF, or WebP image.";
  }
  if (file.size > EDITOR_IMAGE_MAX_BYTES) {
    return "Images must be 10 MB or smaller.";
  }
  return null;
}
