import { forwardRef, type ReactNode } from "react";
import { cn } from "@/lib/utils";
import { TonalIcon, type TonalIconTone } from "./tonal-icon";

/** Section dividers inside the Vault settings ledger. The parent owns its panel. */
export const SettingsSection = forwardRef<HTMLElement, {
  id: string;
  title: string;
  children: ReactNode;
  className?: string;
  layout?: "ledger" | "form" | "card";
  description?: string;
  icon?: ReactNode;
  tone?: TonalIconTone;
  headingLevel?: 2 | 3;
}>(({ id, title, children, className, layout = "ledger", description, icon, tone = "neutral", headingLevel = 3 }, ref) => {
  const Heading = `h${headingLevel}` as "h2" | "h3";
  return (
  <section ref={ref} id={id} role="region" aria-labelledby={`settings-${id}-heading`} className={cn(
    "@container/settings-section min-w-0 scroll-mt-4",
    layout === "form" && "grid gap-5 p-4 sm:p-5 @[64rem]/vault-settings:grid-cols-[13rem_minmax(0,1fr)] @[64rem]/vault-settings:gap-6",
    className,
  )}>
    {layout === "card" ? <>
      <header data-slot="settings-heading" className="flex items-start gap-2.5 border-b border-border p-5">
        {icon && <TonalIcon tone={tone} size="sm">{icon}</TonalIcon>}
        <div className="min-w-0">
          <Heading id={`settings-${id}-heading`} tabIndex={-1} className="text-sm font-semibold leading-5 text-foreground outline-none">{title}</Heading>
          {description && <p className="mt-1 text-xs leading-5 text-foreground-muted">{description}</p>}
        </div>
      </header>
      <div data-slot="settings-group" className="min-w-0">{children}</div>
    </> : layout === "form" ? <>
      <div className="flex items-start gap-2.5">
        {icon && <TonalIcon tone={tone} size="sm">{icon}</TonalIcon>}
        <div className="min-w-0">
          <Heading id={`settings-${id}-heading`} className="text-sm font-semibold leading-5 text-foreground">{title}</Heading>
          {description && <p className="mt-1 text-xs leading-5 text-foreground-muted">{description}</p>}
        </div>
      </div>
      <div data-slot="settings-group" className="@container/settings-values min-w-0">{children}</div>
    </> : <>
    <Heading id={`settings-${id}-heading`} tabIndex={-1} className={cn(
      "border-b border-border bg-background/60 px-4 py-2.5 text-ui font-semibold sm:px-5",
      id === "danger" ? "text-destructive" : "text-foreground",
    )}>{title}</Heading>
    <div data-slot="settings-group" className="divide-y divide-border">{children}</div>
    </>}
  </section>
  );
});
SettingsSection.displayName = "SettingsSection";

/** A plain label/control row; no nested card, label fill or vertical borders. */
export function SettingRow({ label, children }: { label: ReactNode; children: ReactNode }) {
  return (
    <div data-slot="settings-row" className="grid min-w-0 gap-3 px-4 py-4 sm:px-5 @[48rem]/settings-section:grid-cols-[11rem_minmax(0,1fr)] @[48rem]/settings-section:gap-x-6">
      <div data-slot="settings-label" className="min-w-0 text-ui font-medium text-foreground">
        {label}
      </div>
      <div data-slot="settings-value" className="min-w-0">
        <div className="w-full min-w-0">{children}</div>
      </div>
    </div>
  );
}
