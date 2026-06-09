import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Check, Monitor, Moon, Sun } from "lucide-react";
import { useTheme, type Theme } from "@/hooks/use-theme";

const ICONS: Record<Theme, React.ComponentType<{ className?: string; "aria-hidden"?: boolean }>> = {
  light: Sun,
  dark: Moon,
  system: Monitor,
};

const LABELS: Record<Theme, string> = {
  light: "Light",
  dark: "Dark",
  system: "System",
};

export function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const Icon = ICONS[theme];
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger
        aria-label={`Theme: ${LABELS[theme]}`}
        className="inline-flex h-9 w-9 items-center justify-center rounded-[var(--radius-md)] border border-border bg-surface text-foreground hover:bg-surface-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background transition-token cursor-pointer"
      >
        <Icon className="h-4 w-4" aria-hidden />
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={4}
          className="z-50 min-w-[140px] rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md"
        >
          {/* RadioGroup → each item is role=menuitemradio with aria-checked, so
              the active theme is exposed to assistive tech (not just an "ON"
              text marker). The check is teal, not the low-contrast orange. */}
          <DropdownMenu.RadioGroup value={theme} onValueChange={(v) => setTheme(v as Theme)}>
            {(["light", "dark", "system"] as const).map((opt) => {
              const OptIcon = ICONS[opt];
              return (
                <DropdownMenu.RadioItem
                  key={opt}
                  value={opt}
                  className="flex cursor-pointer items-center gap-2 px-3 py-2 text-sm text-foreground outline-none data-[highlighted]:bg-surface-muted"
                >
                  <OptIcon className="h-4 w-4" aria-hidden />
                  <span>{LABELS[opt]}</span>
                  <DropdownMenu.ItemIndicator className="ml-auto">
                    <Check className="h-4 w-4 text-primary" aria-hidden />
                  </DropdownMenu.ItemIndicator>
                </DropdownMenu.RadioItem>
              );
            })}
          </DropdownMenu.RadioGroup>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
