import type { LucideIcon } from "lucide-react";
import { TonalIcon, type TonalIconTone } from "@/components/ui/tonal-icon";

export function SettingsSectionHeader({
  id,
  icon: Icon,
  title,
  description,
  tone = "neutral",
}: {
  id: string;
  icon: LucideIcon;
  title: string;
  description: string;
  tone?: TonalIconTone;
}) {
  return (
    <div
      data-slot="settings-section-header"
      className="border-b border-border-strong bg-surface-2/55 px-4 py-3"
    >
      <div className="flex items-start gap-3">
        <TonalIcon tone={tone}>
          <Icon className="h-4 w-4" aria-hidden />
        </TonalIcon>
        <div className="min-w-0 pt-0.5">
          <h2 id={id} className="text-sm font-semibold text-foreground">
            {title}
          </h2>
          <p className="mt-1 text-xs leading-relaxed text-foreground-muted">
            {description}
          </p>
        </div>
      </div>
    </div>
  );
}
