import { Check, Monitor, Moon, Sun } from "lucide-react";
import { cn } from "@/lib/utils";
import { useTheme, type Theme } from "@/hooks/use-theme";

const themeOptions = [
  { value: "system", label: "System", description: "Match your device", icon: Monitor },
  { value: "light", label: "Light", description: "A bright workspace", icon: Sun },
  { value: "dark", label: "Dark", description: "A low-glare workspace", icon: Moon },
] satisfies Array<{ value: Theme; label: string; description: string; icon: typeof Sun }>;

function ThemePreview({ dark }: { dark: boolean }) {
  return (
    <span className={cn("flex h-full w-full overflow-hidden", dark ? "dark bg-surface" : "bg-primary-foreground")}>
      <span className={cn("flex w-1/4 flex-col gap-2 border-r border-border p-2.5", dark ? "bg-surface-2" : "bg-primary-foreground")}>
        <span className="mb-1 h-3 w-3 rounded-[var(--radius-sm)] bg-primary" />
        <span className="h-1.5 w-full rounded-full bg-primary/30" />
        <span className="h-1.5 w-3/4 rounded-full bg-subtle/30" />
        <span className="h-1.5 w-full rounded-full bg-subtle/30" />
      </span>
      <span className="flex min-w-0 flex-1 flex-col gap-2 p-3">
        <span className="h-2 w-1/2 rounded-full bg-primary" />
        <span className="h-1.5 w-3/4 rounded-full bg-subtle/30" />
        <span className="mt-1 flex gap-2">
          <span className="h-8 flex-1 rounded-[var(--radius-sm)] border border-border" />
          <span className="h-8 flex-1 rounded-[var(--radius-sm)] border border-border" />
        </span>
      </span>
    </span>
  );
}

export function PreferencesSection() {
  const { theme, setTheme } = useTheme();
  return (
    <section aria-labelledby="appearance-heading" className="max-w-3xl">
      <div className="border-b border-border pb-4">
        <h2 id="appearance-heading" className="text-base font-semibold text-foreground">Appearance</h2>
        <p id="appearance-description" className="mt-1 text-sm text-foreground-muted">Choose a theme for this browser. Changes apply immediately.</p>
      </div>
      <fieldset aria-describedby="appearance-description" className="mt-5 grid min-w-0 gap-3 sm:grid-cols-3">
        <legend className="sr-only">Theme</legend>
        {themeOptions.map(({ value, label, description, icon: Icon }) => (
          <label key={value} className="group relative min-w-0 cursor-pointer">
            <input type="radio" name="appearance-theme" value={value} checked={theme === value} onChange={() => setTheme(value)} aria-label={label} className="peer sr-only" />
            <span className="block overflow-hidden rounded-[var(--radius-md)] border border-border bg-surface transition-token group-hover:border-border-strong peer-checked:border-primary peer-checked:ring-1 peer-checked:ring-primary peer-focus-visible:ring-2 peer-focus-visible:ring-ring peer-focus-visible:ring-offset-2 peer-focus-visible:ring-offset-surface">
              <span className="relative block h-28 overflow-hidden border-b border-border" aria-hidden="true">
                <ThemePreview dark={value === "dark"} />
                {value === "system" && <span className="absolute inset-0 [clip-path:polygon(50%_0,100%_0,100%_100%,50%_100%)]"><ThemePreview dark /></span>}
              </span>
              <span className="flex items-center gap-2 px-3 py-3">
                <Icon className="h-4 w-4 shrink-0 text-foreground-muted" aria-hidden="true" />
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-medium text-foreground">{label}</span>
                  <span className="mt-0.5 block text-xs text-foreground-muted">{description}</span>
                </span>
                <span aria-hidden="true" className={cn("flex h-4 w-4 shrink-0 items-center justify-center rounded-full border", theme === value ? "border-primary bg-primary text-primary-foreground" : "border-border-strong")}>
                  {theme === value && <Check className="h-3 w-3" />}
                </span>
              </span>
            </span>
          </label>
        ))}
      </fieldset>
    </section>
  );
}
