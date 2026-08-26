import { Boxes, House, Search, Settings2, type LucideIcon } from "lucide-react";
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

export function AppSidebar({ compact }: { compact: boolean }) {
  const { pathname } = useLocation();

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
        <nav
          aria-label="Workspace navigation"
          className={cn("flex flex-col gap-1 p-2", compact && "items-center")}
        >
          {!compact && <p className="coord px-2 pb-1 pt-1.5">Workspace</p>}
          {PRIMARY_ITEMS.map((item) => (
            <AppSidebarLink
              key={item.to}
              {...item}
              compact={compact}
              selected={item.active(pathname)}
            />
          ))}
        </nav>

        <nav
          aria-label="Account navigation"
          className={cn(
            "mt-auto border-t border-border p-2",
            compact && "flex justify-center",
          )}
        >
          <AppSidebarLink
            to="/settings"
            label="Account settings"
            icon={Settings2}
            compact={compact}
            selected={pathname === "/settings"}
          />
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
