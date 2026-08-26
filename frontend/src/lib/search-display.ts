const CHUNK_CONTEXT_PREFIX = /^\s*\[#[\s\S]*?\]\s*/;
const MARKDOWN_HEADING = /(^|\n)\s*#{1,6}\s+/g;
const MARKDOWN_LIST_MARKER = /(^|\n)\s*[*+-]\s+/g;

export function cleanSearchContext(value?: string | null): string | null {
  if (!value) return null;
  const cleaned = value
    .replace(CHUNK_CONTEXT_PREFIX, "")
    .replace(MARKDOWN_HEADING, "$1")
    .replace(MARKDOWN_LIST_MARKER, "$1")
    .replace(/\s+/g, " ")
    .trim();
  return cleaned || null;
}

export function safeSearchTags(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const normalized = value
    .filter((tag): tag is string => typeof tag === "string")
    .map((tag) => tag.trim())
    .filter(Boolean);
  return [...new Set(normalized)];
}
