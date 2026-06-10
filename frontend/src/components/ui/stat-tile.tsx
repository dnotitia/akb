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
  className,
}: {
  label: string;
  value: number | string;
  kind?: string;
  pad?: boolean;
  className?: string;
}) {
  const display = String(value);
  return (
    <div
      className={cn(
        "rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm",
        pad && "px-4 py-3.5",
        className,
      )}
    >
      <Eyebrow className="mb-1.5 block">{label}</Eyebrow>
      <div className="font-display text-[30px] leading-none tabular-nums text-foreground mb-1.5">
        {display}
      </div>
      {kind && <Eyebrow className="block">{kind}</Eyebrow>}
    </div>
  );
}
