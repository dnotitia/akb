import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Monitor, Moon, Sun } from "lucide-react";
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
        className="inline-flex h-9 w-9 items-center justify-center border border-border bg-surface text-foreground hover:bg-surface-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background transition-colors cursor-pointer"
      >
        <Icon className="h-4 w-4" aria-hidden />
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={4}
          className="z-50 min-w-[140px] border border-border bg-surface p-1 shadow-none"
        >
          {(["light", "dark", "system"] as const).map((opt) => {
            const OptIcon = ICONS[opt];
            return (
              <DropdownMenu.Item
                key={opt}
                onSelect={() => setTheme(opt)}
                className="flex cursor-pointer items-center gap-2 px-3 py-2 text-sm text-foreground outline-none data-[highlighted]:bg-surface-muted"
              >
                <OptIcon className="h-4 w-4" aria-hidden />
                <span>{LABELS[opt]}</span>
                {theme === opt && <span className="coord-spark ml-auto">ON</span>}
              </DropdownMenu.Item>
            );
          })}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
