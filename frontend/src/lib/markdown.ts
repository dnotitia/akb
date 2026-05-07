export interface Heading {
  level: number;
  text: string;
  slug: string;
}

/**
 * Slugify heading text — mirrors the rules GitHub uses for markdown
 * anchors closely enough for our purposes. Preserves Hangul (가-힣) so
 * Korean-only headings produce readable URLs.
 */
export function slugify(text: string): string {
  return text
    .toLowerCase()
    .trim()
    .replace(/[`*_~]/g, "")
    .replace(/[^\w\s가-힣-]/g, "")
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
}

/**
 * Extract ATX (`# Title`) and setext (`Title\n===`) headings from raw
 * markdown. Fenced code blocks are ignored so `# comment` inside a snippet
 * doesn't pollute the outline, and slugs are made unique by appending
 * `-2`, `-3`, ... on collision.
 *
 * Setext detection needs the next line, so we walk the lines with explicit
 * lookahead rather than line-by-line map().
 */
export function parseHeadings(md: string): Heading[] {
  if (!md) return [];
  const lines = md.split("\n").map((l) => l.replace(/\r$/, ""));
  const seen = new Map<string, number>();
  const out: Heading[] = [];
  let inFence = false;

  const push = (level: number, text: string) => {
    const clean = text.trim();
    if (!clean) return;
    const base = slugify(clean) || `heading-${out.length + 1}`;
    const n = (seen.get(base) ?? 0) + 1;
    seen.set(base, n);
    const slug = n === 1 ? base : `${base}-${n}`;
    out.push({ level, text: clean, slug });
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // Fence toggles aren't headings but we still need to track them.
    if (/^\s{0,3}```/.test(line) || /^\s{0,3}~~~/.test(line)) {
      inFence = !inFence;
      continue;
    }
    if (inFence) continue;

    // ATX: up to three leading spaces, 1-6 hashes, a space, the text.
    const atx = /^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
    if (atx) {
      push(atx[1].length, atx[2]);
      continue;
    }

    // Setext: non-empty line followed by === (H1) or --- (H2).
    const next = lines[i + 1];
    if (next && line.trim() && /^\s{0,3}(=+|-+)\s*$/.test(next)) {
      const isH1 = next.trim().startsWith("=");
      push(isH1 ? 1 : 2, line);
      i++; // consume the underline
      continue;
    }
  }
  return out;
}
