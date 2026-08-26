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
      className="mb-2.5 border-b border-border px-0.5 pb-3"
    >
      <div className="flex items-start gap-3">
        <TonalIcon tone={tone}>
          <Icon className="h-4 w-4" aria-hidden />
        </TonalIcon>
        <div className="min-w-0">
          <h2
            id={id}
            className="font-display text-base font-semibold tracking-tight text-foreground"
          >
            {title}
          </h2>
          <p className="mt-0.5 text-xs leading-relaxed text-foreground-muted">
            {description}
          </p>
        </div>
      </div>
    </div>
  );
}
