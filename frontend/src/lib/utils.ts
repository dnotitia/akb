import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "-";
  return new Date(iso).toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

/**
 * Allowlist URL schemes safe for `<a href>`. Rejects `javascript:`,
 * `data:`, `vbscript:`, and anything else not explicitly enumerated so
 * markdown like `[click](javascript:alert(1))` can't execute when a user
 * activates the link in either the editor or the rendered view.
 */
export function sanitizeLinkUrl(raw: string | null | undefined): string {
  if (!raw) return "#";
  const trimmed = raw.trim();
  if (!trimmed) return "#";
  // Protocol-relative URLs (`//evil.com/x`) inherit the current page
  // scheme and act like a redirect to an arbitrary origin. Treat them
  // as untrusted and refuse before the absolute-path check below.
  if (trimmed.startsWith("//")) return "#";
  if (trimmed.startsWith("/") || trimmed.startsWith("#") || trimmed.startsWith("?")) {
    return trimmed;
  }
  const lower = trimmed.toLowerCase();
  if (
    lower.startsWith("http://") ||
    lower.startsWith("https://") ||
    lower.startsWith("mailto:") ||
    lower.startsWith("tel:")
  ) {
    return trimmed;
  }
  return "#";
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "-";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "-"; // corrupt/non-ISO → fallback, never "NaNm ago"
  const diff = Date.now() - t;
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  // Past a week, collapse the dormant tail to coarse buckets so a directory
  // of old vaults reads "8mo ago", not a two/three-digit "247d ago" sitting
  // next to a fresh feed's "3h ago". The recent grammar (just now / m / h / d)
  // is preserved for the first week — the window that actually matters.
  // Day-thresholds (not floor-then-compare) so there's no 360–364d gap that
  // would fall through to "0y ago".
  if (days < 7) return `${days}d ago`;
  if (days < 30) return `${Math.floor(days / 7)}w ago`;
  if (days < 365) return `${Math.floor(days / 30)}mo ago`;
  return `${Math.floor(days / 365)}y ago`;
}

/**
 * Whether a timestamp is "fresh" — changed within the window (default 1h).
 * Drives the single sanctioned warm accent on a just-touched row; it decays
 * naturally as the change ages, so there's never a permanent "NEW" badge.
 */
export function isFresh(iso: string | null | undefined, withinMs = 3_600_000): boolean {
  if (!iso) return false;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return false;
  return Date.now() - t < withinMs;
}

/**
 * Deterministic string → hue in [0,360) (FNV-1a). Same key always maps to the
 * same hue regardless of how many keys exist, so a vault keeps one identity
 * color across Recent activity, the directory, and the graph clusters (which
 * bucket this hue into the same categorical ramp via groupColor).
 */
export function hashHue(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0) % 360;
}
