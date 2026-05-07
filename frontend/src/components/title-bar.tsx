import { Link } from "react-router-dom";
import { Compass, GitGraph, Search as SearchIcon, Share2 } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface Crumb {
  label: string;
  to?: string;
}

export function TitleBar({
  crumbs,
  right,
  className,
}: {
  crumbs: Crumb[];
  right?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex items-center gap-2.5 h-9 px-4 border-b border-border bg-surface",
        "font-mono text-[10px] uppercase tracking-wider text-foreground-muted",
        className,
      )}
    >
      <span
        className="inline-block h-2 w-2 rounded-full bg-accent"
        aria-hidden
      />
      <Link
        to="/"
        className="text-foreground-muted hover:text-foreground transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
      >
        AKB
      </Link>
      {crumbs.map((c, i) => (
        <span key={i} className="flex items-center gap-2.5">
          <span className="text-foreground-muted">›</span>
          {c.to ? (
            <Link
              to={c.to}
              className={cn(
                "transition-colors hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface",
                i === crumbs.length - 1 ? "text-foreground" : "text-foreground-muted",
              )}
            >
              {c.label}
            </Link>
          ) : (
            <span
              className={cn(
                i === crumbs.length - 1 ? "text-foreground" : "text-foreground-muted",
              )}
            >
              {c.label}
            </span>
          )}
        </span>
      ))}
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
    ["overview", "OVERVIEW", `/vault/${vault}`, Compass],
    ["search", "SEARCH", `/vault/${vault}/search`, SearchIcon],
    ["graph", "GRAPH", `/vault/${vault}/graph`, GitGraph],
    ["publish", "PUBLISH", `/vault/${vault}/publications`, Share2],
  ];
  return (
    <div className="flex items-center gap-1">
      {actions.map(([k, label, href, Icon]) => {
        const active = k === page;
        const accent = k === "graph";
        return (
          <Link
            key={k}
            to={href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "inline-flex items-center gap-1 px-2 h-6 border transition-colors",
              "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface",
              active && accent
                ? "border-accent bg-accent/10 text-accent"
                : active
                  ? "border-foreground-muted bg-surface-muted text-foreground"
                  : "border-border text-foreground-muted hover:text-foreground hover:bg-surface-muted",
            )}
          >
            <Icon className="h-3 w-3" aria-hidden />
            {label}
          </Link>
        );
      })}
    </div>
  );
}
