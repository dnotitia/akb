import { Check, Monitor, Moon, Sun } from "lucide-react";
import { cn } from "@/lib/utils";
import { useTheme, type Theme } from "@/hooks/use-theme";
import { SettingsHeading, SettingsGroup } from "./settings-ui";

const themeOptions = [
  { value: "system", label: "System", description: "Match your device", icon: Monitor },
  { value: "light", label: "Light", description: "A bright workspace", icon: Sun },
  { value: "dark", label: "Dark", description: "A low-glare workspace", icon: Moon },
] satisfies Array<{ value: Theme; label: string; description: string; icon: typeof Sun }>;

function ThemePreview({ dark }: { dark: boolean }) {
  return (
    <span className={cn("settings-theme-preview", dark && "settings-theme-preview-dark")}>
      <span className="settings-theme-preview-rail"><i /><i /><i /><i /></span>
      <span className="settings-theme-preview-page"><i /><i /><span><i /><i /></span><i /></span>
    </span>
  );
}

export function PreferencesSection() {
  const { theme, setTheme } = useTheme();
  return (
    <section aria-labelledby="appearance-heading" className="@container/appearance min-w-0">
      <SettingsHeading id="appearance-heading" title="Appearance" description="Make this workspace comfortable to spend time in." />
      <SettingsGroup id="theme-heading" title="Interface theme" description="Choose a theme for this browser. Changes apply immediately.">
      <fieldset aria-labelledby="theme-heading" className="settings-theme-options">
        <legend className="sr-only">Theme</legend>
        {themeOptions.map(({ value, label, description, icon: Icon }) => (
          <label key={value} className="group relative min-w-0 cursor-pointer">
            <input type="radio" name="appearance-theme" value={value} checked={theme === value} onChange={() => setTheme(value)} aria-label={label} className="peer sr-only" />
            <span className="settings-theme-option">
              <span className="settings-theme-thumbnail" aria-hidden="true">
                <ThemePreview dark={value === "dark"} />
                {value === "system" && <span className="absolute inset-0 [clip-path:polygon(50%_0,100%_0,100%_100%,50%_100%)]"><ThemePreview dark /></span>}
              </span>
              <span className="flex min-w-0 items-center gap-2 p-3">
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
      </SettingsGroup>
      <p className="settings-browser-note"><Monitor aria-hidden />This preference is saved on this device. Your documents and shared links keep their original formatting.</p>
    </section>
  );
}
