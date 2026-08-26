import { Check, Monitor, Moon, Sun } from "lucide-react";
import { Panel } from "@/components/ui/panel";
import { cn } from "@/lib/utils";
import { useTheme, type Theme } from "@/hooks/use-theme";

const themeOptions: Array<{
  value: Theme;
  label: string;
  description: string;
  icon: typeof Sun;
}> = [
  {
    value: "system",
    label: "System",
    description: "Follow your operating system automatically.",
    icon: Monitor,
  },
  {
    value: "light",
    label: "Light",
    description: "Bright paper surfaces with crisp contrast.",
    icon: Sun,
  },
  {
    value: "dark",
    label: "Dark",
    description: "Deep slate surfaces for low-glare reading.",
    icon: Moon,
  },
];

export function PreferencesSection() {
  const { theme, setTheme } = useTheme();
  const activeTheme = themeOptions.find((option) => option.value === theme) ?? themeOptions[0];

  return (
    <Panel>
      <div className="border-b border-border px-5 py-5 sm:px-6">
        <h2 id="appearance-heading" className="text-base font-semibold text-foreground">Appearance</h2>
        <p className="mt-1 text-sm text-foreground-muted">
          Choose how AKB looks on this browser. Changes apply immediately.
        </p>
      </div>
      <div className="grid xl:grid-cols-[minmax(0,1fr)_22rem]">
        <div
          role="radiogroup"
          aria-labelledby="appearance-heading"
          className="space-y-3 p-5 sm:p-6"
        >
          {themeOptions.map((option) => {
            const active = theme === option.value;
            const Icon = option.icon;
            return (
              <button
                key={option.value}
                type="button"
                role="radio"
                aria-checked={active}
                onClick={() => setTheme(option.value)}
                className={cn(
                  "group flex min-h-20 w-full cursor-pointer items-center gap-4 rounded-[var(--radius-lg)] border px-4 py-3 text-left transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface",
                  active
                    ? "border-primary bg-surface-selected text-surface-selected-foreground shadow-sm"
                    : "border-border bg-surface text-foreground hover:border-border-strong hover:bg-surface-hover",
                )}
              >
                <span
                  className={cn(
                    "flex h-10 w-10 shrink-0 items-center justify-center rounded-[var(--radius-md)] border",
                    active
                      ? "border-primary/20 bg-surface text-primary"
                      : "border-border bg-surface-2 text-foreground-muted group-hover:text-foreground",
                  )}
                >
                  <Icon className="h-4 w-4" aria-hidden />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-semibold">{option.label}</span>
                  <span className={cn("mt-1 block text-xs leading-relaxed", active ? "text-surface-selected-foreground" : "text-foreground-muted")}>
                    {option.description}
                  </span>
                </span>
                <span
                  className={cn(
                    "flex h-5 w-5 shrink-0 items-center justify-center rounded-full border",
                    active ? "border-primary bg-primary text-primary-foreground" : "border-border bg-surface",
                  )}
                  aria-hidden
                >
                  {active && <Check className="h-3 w-3" />}
                </span>
              </button>
            );
          })}
        </div>

        <aside className="border-t border-border bg-surface-2/60 p-5 sm:p-6 xl:border-l xl:border-t-0">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h3 className="text-sm font-semibold text-foreground">Interface preview</h3>
              <p className="mt-1 text-xs text-foreground-muted">A compact view of the selected canvas.</p>
            </div>
            <span className="rounded-full border border-border bg-surface px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-foreground-muted">
              {activeTheme.label}
            </span>
          </div>

          <div className="mt-5 overflow-hidden rounded-[var(--radius-lg)] border border-border-strong bg-background shadow-md" aria-hidden>
            <div className="flex h-9 items-center gap-1.5 border-b border-border bg-surface px-3">
              <span className="h-2 w-2 rounded-full bg-primary" />
              <span className="h-2 w-2 rounded-full bg-accent" />
              <span className="h-2 w-2 rounded-full bg-border-strong" />
              <span className="ml-auto h-4 w-20 rounded bg-surface-2" />
            </div>
            <div className="grid h-44 grid-cols-[3.75rem_minmax(0,1fr)]">
              <div className="space-y-2 border-r border-border bg-surface px-2 py-3">
                <span className="block h-6 rounded bg-surface-selected" />
                <span className="block h-6 rounded bg-surface-2" />
                <span className="block h-6 rounded bg-surface-2" />
              </div>
              <div className="p-4">
                <span className="block h-2.5 w-16 rounded bg-primary" />
                <span className="mt-3 block h-4 w-28 rounded bg-foreground/15" />
                <span className="mt-2 block h-2.5 w-36 max-w-full rounded bg-foreground/10" />
                <div className="mt-5 grid grid-cols-2 gap-2">
                  <span className="h-14 rounded-[var(--radius-md)] border border-border bg-surface shadow-xs" />
                  <span className="h-14 rounded-[var(--radius-md)] border border-border bg-surface shadow-xs" />
                </div>
                <span className="mt-3 block h-7 w-20 rounded-[var(--radius-sm)] bg-primary" />
              </div>
            </div>
          </div>
        </aside>
      </div>
    </Panel>
  );
}
