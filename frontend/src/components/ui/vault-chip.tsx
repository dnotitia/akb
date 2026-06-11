import { cn, hashHue } from "@/lib/utils";

// The CVD-vetted categorical ramp, as CSS tokens so the chip stays theme-aware
// (no light/dark fork here). hashHue buckets a vault name deterministically
// into one swatch, so the SAME vault wears the SAME color everywhere it
// appears — Recent activity, the vault directory, and (by the same hash) the
// graph clusters. Identity, not type: it's keyed on the name, not the content.
const CAT_VARS = [
  "var(--color-cat-1)",
  "var(--color-cat-2)",
  "var(--color-cat-3)",
  "var(--color-cat-4)",
  "var(--color-cat-5)",
  "var(--color-cat-6)",
];

function catVar(name: string): string {
  return CAT_VARS[hashHue(name) % CAT_VARS.length];
}

// 1–2 letters: initials across word/separator boundaries (akb-platform → "AP"),
// otherwise the first two characters. A presentational mark only — the readable
// vault name always sits beside it, so the chip itself is aria-hidden.
function initials(name: string): string {
  const cleaned = name.replace(/[^\p{L}\p{N}]+/gu, " ").trim();
  if (!cleaned) return name.slice(0, 2).toUpperCase();
  const parts = cleaned.split(/\s+/);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return cleaned.slice(0, 2).toUpperCase();
}

/**
 * A small flat tinted monogram tile for a vault. Deliberately NOT a glossy
 * avatar or a gradient feat-* hero — a quiet identity anchor that the readable
 * name leads. `sm` rides inline in a Recent row; `md` anchors a directory row.
 */
export function VaultChip({
  name,
  size = "sm",
  className,
}: {
  name: string;
  size?: "sm" | "md";
  className?: string;
}) {
  const color = catVar(name);
  const dim = size === "md" ? "h-7 w-7 text-[11px]" : "h-5 w-5 text-[9px]";
  return (
    <span
      aria-hidden
      className={cn(
        "inline-flex shrink-0 items-center justify-center rounded-[var(--radius-sm)] font-semibold leading-none",
        dim,
        className,
      )}
      style={{
        color,
        backgroundColor: `color-mix(in srgb, ${color} 14%, transparent)`,
      }}
    >
      {initials(name)}
    </span>
  );
}
