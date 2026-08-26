import { Box } from "lucide-react";
import { CopyButton } from "@/components/ui/copy-button";
import { cn } from "@/lib/utils";

/** A compact, consistent Vault identity marker for workspace headers. */
export function VaultContextBadge({
  name,
  address = false,
  copyable = false,
  className,
}: {
  name: string;
  address?: boolean;
  copyable?: boolean;
  className?: string;
}) {
  const value = address ? `akb://${name}` : name;
  return (
    <span
      className={cn(
        "inline-flex min-h-6 max-w-full items-center gap-1.5 rounded-[var(--radius-sm)] border border-border bg-surface-2 px-2 text-xs text-foreground-muted",
        address && "font-mono",
        className,
      )}
    >
      <Box className="h-3.5 w-3.5 shrink-0" aria-hidden />
      <span className="truncate" title={value}>
        {value}
      </span>
      {copyable && (
        <CopyButton value={value} label={`Copy ${value}`} />
      )}
    </span>
  );
}
