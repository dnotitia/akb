import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { Panel } from "@/components/ui/panel";
import { TonalIcon, type TonalIconTone } from "@/components/ui/tonal-icon";
import { cn } from "@/lib/utils";

export function ResourceWorkspace({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <section
      aria-label={label}
      className="flex h-full min-h-0 flex-col overflow-hidden bg-background fade-in"
    >
      {children}
    </section>
  );
}

export function ResourceWorkspaceHeader({
  icon: Icon,
  iconTone,
  title,
  subtitle,
  meta,
  actions,
}: {
  icon: LucideIcon;
  iconTone: TonalIconTone;
  title: ReactNode;
  subtitle: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="relative z-[var(--z-sticky)] flex h-16 shrink-0 items-center gap-3 border-b border-border bg-surface px-3 sm:px-4 lg:px-5">
      <TonalIcon tone={iconTone} size="lg" className="hidden sm:inline-flex">
        <Icon aria-hidden />
      </TonalIcon>
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-2">
          <h1 className="truncate font-display text-base font-semibold text-foreground sm:text-lg">
            {title}
          </h1>
          {meta}
        </div>
        <p className="truncate text-xs text-foreground-muted">{subtitle}</p>
      </div>
      {actions && (
        <div className="ml-auto flex shrink-0 items-center gap-2">{actions}</div>
      )}
    </header>
  );
}

export function ResourceCanvas({ children }: { children: ReactNode }) {
  return (
    <main className="h-full overflow-hidden bg-background p-2 sm:p-3">
      <div className="flex h-full min-h-0 flex-col">{children}</div>
    </main>
  );
}

export function ResourceContextBar({
  children,
  trailing,
}: {
  children: ReactNode;
  trailing?: ReactNode;
}) {
  return (
    <div className="mb-3 flex min-h-11 shrink-0 items-center gap-3 rounded-[var(--radius-lg)] border border-border bg-surface px-3 shadow-xs">
      <div className="min-w-0 flex-1">{children}</div>
      {trailing && (
        <div className="flex shrink-0 items-center gap-3 text-xs text-foreground-muted">
          {trailing}
        </div>
      )}
    </div>
  );
}

export function ResourceViewerFrame({
  icon: Icon,
  label,
  meta,
  children,
  className,
  bodyClassName,
}: {
  icon: LucideIcon;
  label: ReactNode;
  meta?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <Panel
      variant="workspace"
      className={cn(
        "flex min-h-0 flex-1 flex-col rounded-[var(--radius-lg)] shadow-sm",
        className,
      )}
    >
      <div className="flex min-h-11 shrink-0 items-center gap-2 border-b border-border bg-surface-2/60 px-3">
        <Icon className="h-3.5 w-3.5 text-link" aria-hidden />
        <span className="text-xs font-semibold text-foreground">{label}</span>
        {meta && (
          <div className="ml-auto flex min-w-0 items-center gap-3 text-xs text-foreground-muted">
            {meta}
          </div>
        )}
      </div>
      <div className={cn("min-h-0 flex-1 bg-surface", bodyClassName)}>{children}</div>
    </Panel>
  );
}
