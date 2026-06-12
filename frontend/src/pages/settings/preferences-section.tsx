import { Monitor, Moon, Sun } from "lucide-react";
import { Segmented } from "@/components/ui/segmented";
import { useTheme, type Theme } from "@/hooks/use-theme";

export function PreferencesSection() {
  const { theme, setTheme } = useTheme();
  return (
    <div className="rounded-[var(--radius-lg)] border border-border bg-surface shadow-sm overflow-hidden">
      <header className="border-b border-border px-6 py-3">
        <span id="theme-label" className="coord-ink">Theme</span>
      </header>
      <div className="p-6 max-w-sm">
        <Segmented
          aria-labelledby="theme-label"
          value={theme}
          onChange={(v) => setTheme(v as Theme)}
          className="grid-cols-3"
          options={[
            { value: "light", label: "Light", icon: <Sun className="h-3 w-3" aria-hidden /> },
            { value: "dark", label: "Dark", icon: <Moon className="h-3 w-3" aria-hidden /> },
            { value: "system", label: "System", icon: <Monitor className="h-3 w-3" aria-hidden /> },
          ]}
        />
        <p className="text-xs text-foreground-muted mt-2 leading-relaxed">
          {theme === "system"
            ? "Follows your operating system's appearance."
            : `Always ${theme}.`}
        </p>
      </div>
    </div>
  );
}
