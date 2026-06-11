import { cn } from "@/lib/utils";
import { Eyebrow } from "@/components/ui/eyebrow";

/**
 * Design-system primitive: a labelled metric tile (rounded surface, big
 * tabular numeral). Centralizes the stat-strip look used on vault/dashboard.
 */
export function StatTile({
  label,
  value,
  kind,
  pad = true,
  dimZero = false,
  className,
}: {
  label: string;
  value: number | string;
  kind?: string;
  pad?: boolean;
  /** Opt-in: recede the numeral when the value is exactly 0 so empty
   *  categories read as "nothing here" rather than competing for attention.
   *  Off by default — existing consumers are unaffected. */
  dimZero?: boolean;
  className?: string;
}) {
  const display = String(value);
  const isZero = dimZero && (value === 0 || value === "0");
  return (
    <div
      className={cn(
        "rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm",
        pad && "px-4 py-3.5",
        className,
      )}
    >
      <Eyebrow className="mb-1.5 block">{label}</Eyebrow>
      <div
        className={cn(
          "font-display text-[30px] leading-none tabular-nums mb-1.5",
          isZero ? "text-foreground-muted" : "text-foreground",
        )}
      >
        {display}
      </div>
      {kind && <Eyebrow className="block">{kind}</Eyebrow>}
    </div>
  );
}
