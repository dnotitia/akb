import {
  Boxes,
  House,
  PanelLeftClose,
  PanelLeftOpen,
  Search,
  type LucideIcon,
} from "lucide-react";
import { Link, useLocation } from "react-router-dom";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

const PRIMARY_ITEMS: Array<{
  to: string;
  label: string;
  icon: LucideIcon;
  active: (pathname: string) => boolean;
}> = [
  {
    to: "/",
    label: "Home",
    icon: House,
    active: (pathname) => pathname === "/",
  },
  {
    to: "/vault",
    label: "Vaults",
    icon: Boxes,
    active: (pathname) => pathname.startsWith("/vault"),
  },
  {
    to: "/search",
    label: "Search",
    icon: Search,
    active: (pathname) => pathname === "/search",
  },
];

export function AppSidebar({
  compact,
  collapsible = false,
  onCompactChange,
}: {
  compact: boolean;
  collapsible?: boolean;
  onCompactChange?: (compact: boolean) => void;
}) {
  const { pathname } = useLocation();
  const toggleLabel = compact ? "Expand sidebar" : "Collapse sidebar";

  return (
    <TooltipProvider delayDuration={250}>
      <aside
        data-testid="app-sidebar"
        data-compact={compact ? "true" : "false"}
        className={cn(
          "sticky top-14 hidden h-[calc(100dvh-3.5rem)] shrink-0 self-start border-r border-border bg-surface lg:flex lg:flex-col",
          compact ? "lg:w-14" : "lg:w-52",
        )}
      >
        {(!compact || collapsible) && (
          <div
            className={cn(
              "flex h-11 shrink-0 items-center border-b border-border px-2",
              compact ? "justify-center" : "justify-between",
            )}
          >
            {!compact && <span className="coord px-2">Workspace</span>}
            {collapsible && onCompactChange && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <button
                    type="button"
                    aria-label={toggleLabel}
                    aria-expanded={!compact}
                    aria-controls="workspace-navigation"
                    onClick={() => onCompactChange(!compact)}
                    className="flex h-8 w-8 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
                  >
                    {compact ? (
                      <PanelLeftOpen className="h-4 w-4" aria-hidden />
                    ) : (
                      <PanelLeftClose className="h-4 w-4" aria-hidden />
                    )}
                  </button>
                </TooltipTrigger>
                <TooltipContent side="right">{toggleLabel}</TooltipContent>
              </Tooltip>
            )}
          </div>
        )}

        <nav
          id="workspace-navigation"
          aria-label="Workspace navigation"
          className={cn("flex flex-col gap-1 p-2", compact && "items-center")}
        >
          {PRIMARY_ITEMS.map((item) => (
            <AppSidebarLink
              key={item.to}
              {...item}
              compact={compact}
              selected={item.active(pathname)}
            />
          ))}
        </nav>
      </aside>
    </TooltipProvider>
  );
}

function AppSidebarLink({
  to,
  label,
  icon: Icon,
  compact,
  selected,
}: {
  to: string;
  label: string;
  icon: LucideIcon;
  compact: boolean;
  selected: boolean;
}) {
  const link = (
    <Link
      to={to}
      aria-label={compact ? label : undefined}
      aria-current={selected ? "page" : undefined}
      className={cn(
        "relative flex h-10 items-center rounded-[var(--radius-md)] text-sm font-medium transition-token focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset",
        compact ? "w-10 justify-center" : "w-full gap-3 px-3",
        selected
          ? "bg-surface-selected text-surface-selected-foreground"
          : "text-foreground-muted hover:bg-surface-hover hover:text-foreground",
      )}
    >
      {selected && (
        <span
          className="absolute bottom-2 left-0 top-2 w-0.5 rounded-full bg-primary"
          aria-hidden
        />
      )}
      <Icon className="h-4 w-4 shrink-0" aria-hidden />
      {!compact && <span>{label}</span>}
    </Link>
  );

  if (!compact) return link;

  return (
    <Tooltip>
      <TooltipTrigger asChild>{link}</TooltipTrigger>
      <TooltipContent side="right">{label}</TooltipContent>
    </Tooltip>
  );
}
