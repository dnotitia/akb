import { useEffect, useRef } from "react";
import { Search as SearchIcon } from "lucide-react";

interface MenuFilterProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  "aria-label"?: string;
}

/**
 * A compact filter input designed to live at the top of a Radix DropdownMenu
 * `Content` (SelectMenu, vault pickers). Radix menus run a typeahead handler at
 * the content level that would otherwise swallow keystrokes, so we stop
 * propagation for everything except Escape (which still closes the menu). Radix
 * focuses the first menu item on open (it exposes no `onOpenAutoFocus` to stop
 * that), so we steal focus back to this input on the next frame — best-effort:
 * if Radix wins the race the user can still click the box.
 */
export function MenuFilter({
  value,
  onChange,
  placeholder = "Filter…",
  "aria-label": ariaLabel,
}: MenuFilterProps) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const raf = requestAnimationFrame(() => ref.current?.focus());
    return () => cancelAnimationFrame(raf);
  }, []);
  return (
    <div className="px-1 pb-1 pt-0.5">
      <div className="relative">
        <SearchIcon
          className="pointer-events-none absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-foreground-muted"
          aria-hidden
        />
        <input
          ref={ref}
          type="search"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            // Let Escape bubble so Radix closes the menu; keep every other key
            // (incl. the menu's typeahead + arrow nav) from hijacking input.
            if (e.key !== "Escape") e.stopPropagation();
          }}
          placeholder={placeholder}
          aria-label={ariaLabel ?? placeholder}
          className="h-8 w-full rounded-[var(--radius-md)] border border-border bg-background pl-6 pr-2 text-xs text-foreground placeholder:text-foreground-muted transition-colors focus:border-primary focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      </div>
    </div>
  );
}
