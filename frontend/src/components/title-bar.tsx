import { Link, useLocation, useNavigate } from "react-router-dom";
import {
  ArrowLeft,
  Compass,
  GitGraph,
  Search as SearchIcon,
  Settings as SettingsIcon,
  Share2,
  Users,
  type LucideIcon,
} from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface Crumb {
  label: string;
  to?: string;
}

export function TitleBar({
  crumbs,
  right,
  left,
  className,
  showBack = true,
}: {
  crumbs: Crumb[];
  right?: ReactNode;
  left?: ReactNode;
  className?: string;
  showBack?: boolean;
}) {
  const navigate = useNavigate();
  const location = useLocation();

  const canBack =
    typeof window !== "undefined" &&
    window.history.length > 1 &&
    location.pathname !== "/";

  function handleBack() {
    if (canBack) navigate(-1);
  }

  return (
    <div
      className={cn(
        "flex h-10 items-center gap-2 overflow-x-auto border-b border-border bg-surface/80 px-3 backdrop-blur [scrollbar-width:none] [&::-webkit-scrollbar]:hidden",
        "text-xs font-medium text-foreground-muted",
        className,
      )}
    >
      {left}
      {showBack && (
        <button
          type="button"
          onClick={handleBack}
          disabled={!canBack}
          aria-label="Go back"
          title="Go back"
          className={cn(
            "-ml-1 inline-flex h-7 w-7 items-center justify-center rounded-[var(--radius-sm)]",
            "text-foreground-muted hover:text-foreground hover:bg-surface-hover",
            "disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:bg-transparent",
            "transition-colors duration-150",
            "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface",
            "cursor-pointer",
          )}
        >
          <ArrowLeft className="h-3 w-3" aria-hidden />
        </button>
      )}
      {crumbs.length > 0 && (
        <nav aria-label="Breadcrumb" className="shrink-0">
          <ol className="flex items-center gap-2">
            {crumbs.map((c, i) => {
              const isLast = i === crumbs.length - 1;
              return (
                <li key={i} className="flex items-center gap-2">
                  {i > 0 && (
                    <span className="text-foreground-muted" aria-hidden>
                      ›
                    </span>
                  )}
                  {c.to ? (
                    <Link
                      to={c.to}
                      aria-current={isLast ? "page" : undefined}
                      className={cn(
                        "transition-colors hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface",
                        isLast ? "text-foreground" : "text-foreground-muted",
                      )}
                    >
                      {c.label}
                    </Link>
                  ) : (
                    <span
                      aria-current={isLast ? "page" : undefined}
                      className={cn(isLast ? "text-foreground" : "text-foreground-muted")}
                    >
                      {c.label}
                    </span>
                  )}
                </li>
              );
            })}
          </ol>
        </nav>
      )}
      {right && <div className="ml-auto flex shrink-0 items-center gap-1">{right}</div>}
    </div>
  );
}

// Activity has no top-level tab, but remains a distinct page kind so it does
// not fall through to Overview. Members and Settings form one governance group
// after the structural divider in VaultActions.
export type VaultPageKind =
  | "overview"
  | "search"
  | "graph"
  | "publish"
  | "members"
  | "settings"
  | "activity";

interface VaultActionsProps {
  vault: string;
  page: VaultPageKind;
}

export function VaultActions({ vault, page }: VaultActionsProps) {
  type VaultAction = [VaultPageKind, string, string, LucideIcon];

  const actions: VaultAction[] = [
    ["overview", "Overview", `/vault/${vault}`, Compass],
    ["search", "Search", `/vault/${vault}/search`, SearchIcon],
    ["graph", "Graph", `/vault/${vault}/graph`, GitGraph],
    ["publish", "Publish", `/vault/${vault}/publications`, Share2],
  ];
  const governanceActions: VaultAction[] = [
    ["members", "Members", `/vault/${vault}/members`, Users],
    ["settings", "Settings", `/vault/${vault}/settings`, SettingsIcon],
  ];

  const renderAction = ([k, label, href, Icon]: (typeof actions)[number]) => {
    const active = k === page;
    return (
      <Link
        key={k}
        to={href}
        aria-current={active ? "page" : undefined}
        className={cn(
          "relative inline-flex h-8 items-center gap-1.5 rounded-[var(--radius-sm)] border border-transparent px-2 text-xs transition-token",
          "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface",
          active
            ? "bg-surface-selected font-semibold text-surface-selected-foreground"
            : "text-foreground-muted hover:bg-surface-hover hover:text-foreground",
        )}
      >
        <Icon className="h-3.5 w-3.5" aria-hidden />
        {label}
      </Link>
    );
  };

  return (
    <nav aria-label="Vault sections" className="flex items-center gap-0.5">
      {actions.map(renderAction)}
      <span className="mx-0.5 h-4 w-px bg-border" aria-hidden />
      {governanceActions.map(renderAction)}
    </nav>
  );
}
