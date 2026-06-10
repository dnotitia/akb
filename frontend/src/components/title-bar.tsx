import { Link, useLocation, useNavigate } from "react-router-dom";
import { ArrowLeft, Compass, GitGraph, Search as SearchIcon, Share2 } from "lucide-react";
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
}: {
  crumbs: Crumb[];
  right?: ReactNode;
  left?: ReactNode;
  className?: string;
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
        "flex items-center gap-2.5 h-10 px-3 border-b border-border bg-surface/80 backdrop-blur",
        "text-xs font-medium text-foreground-muted",
        className,
      )}
    >
      {left}
      <button
        type="button"
        onClick={handleBack}
        disabled={!canBack}
        aria-label="Go back"
        title="Go back"
        className={cn(
          "inline-flex items-center justify-center h-6 w-6 -ml-1",
          "text-foreground-muted hover:text-foreground hover:bg-surface-hover",
          "disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:bg-transparent",
          "transition-colors duration-150",
          "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface",
          "cursor-pointer",
        )}
      >
        <ArrowLeft className="h-3 w-3" aria-hidden />
      </button>
      {crumbs.length > 0 && (
        <nav aria-label="Breadcrumb">
          <ol className="flex items-center gap-2.5">
            {crumbs.map((c, i) => {
              const isLast = i === crumbs.length - 1;
              return (
                <li key={i} className="flex items-center gap-2.5">
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
      {right && <div className="ml-auto flex items-center gap-2">{right}</div>}
    </div>
  );
}

export type VaultPageKind = "overview" | "search" | "graph" | "publish";

interface VaultActionsProps {
  vault: string;
  page: VaultPageKind;
}

export function VaultActions({ vault, page }: VaultActionsProps) {
  const actions: Array<[VaultPageKind, string, string, React.ComponentType<any>]> = [
    ["overview", "Overview", `/vault/${vault}`, Compass],
    ["search", "Search", `/vault/${vault}/search`, SearchIcon],
    ["graph", "Graph", `/vault/${vault}/graph`, GitGraph],
    ["publish", "Publish", `/vault/${vault}/publications`, Share2],
  ];
  return (
    <nav aria-label="Vault sections" className="flex items-center gap-1">
      {actions.map(([k, label, href, Icon]) => {
        const active = k === page;
        return (
          <Link
            key={k}
            to={href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "inline-flex items-center gap-1 px-2.5 h-7 rounded-[var(--radius-sm)] border transition-token",
              "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface",
              // Selection is teal app-wide — no per-tab orange special-case.
              active
                ? "border-transparent bg-surface-selected text-surface-selected-foreground"
                : "border-border text-foreground-muted hover:text-foreground hover:bg-surface-hover",
            )}
          >
            <Icon className="h-3 w-3" aria-hidden />
            {label}
          </Link>
        );
      })}
    </nav>
  );
}
