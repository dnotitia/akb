import { useId, useLayoutEffect } from "react";
import { Link, useLocation } from "react-router-dom";
import { getVaultPageActions } from "@/components/title-bar";
import { useResourceNavigation } from "@/contexts/resource-navigation-context";

/** Overview owns page discovery; breadcrumbs only describe the current location. */
export function VaultOverviewNavigation({ vault, memberCount }: { vault: string; memberCount?: number }) {
  const memberCountId = useId();
  const hasMemberCount = typeof memberCount === "number" && Number.isFinite(memberCount) && memberCount >= 0;
  const { requestNavigation } = useResourceNavigation();
  const actions = getVaultPageActions(vault).filter(action => action.key !== "overview" && action.key !== "search");
  return (
    <nav aria-label="Vault pages" className="min-w-0 max-w-full shrink-0">
      <ul className="flex flex-wrap items-center gap-x-3 gap-y-1">
        {actions.map(action => (
          <li key={action.key} className={action.key === "members" ? "ml-2" : undefined}>
            <Link aria-label={action.key === "members" ? "Members" : undefined} aria-describedby={action.key === "members" && hasMemberCount ? memberCountId : undefined} to={action.href} state={{ vaultPageNavigation: action.href }}
              className="inline-flex min-h-11 min-w-11 items-center gap-1.5 rounded-[var(--radius-sm)] text-ui font-medium text-link transition-colors hover:text-link-hover focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-ring"
              onClick={event => {
                if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.defaultPrevented) return;
                if (!requestNavigation(action.href)) { event.preventDefault(); return; }
              }}>
              <action.icon className="hidden h-3.5 w-3.5 shrink-0 @[30rem]/vault-overview:block" aria-hidden />
              {action.key === "publish" ? "Public links" : action.label}
              {action.key === "members" && hasMemberCount && <span aria-hidden className="text-xs font-normal tabular-nums text-foreground-muted">{memberCount}</span>}
            </Link>
            {action.key === "members" && hasMemberCount && <span id={memberCountId} className="sr-only">{memberCount} members</span>}
          </li>
        ))}
      </ul>
    </nav>
  );
}

/** Observe the committed route in the persistent shell. The Overview can
 * unmount after an animation frame when React defers a route transition. */
export function VaultOverviewNavigationFocus() {
  const location = useLocation();
  useLayoutEffect(() => {
    const destination = location.state?.vaultPageNavigation;
    if (destination === location.pathname
      || (location.state?.collectionView === "main" && destination === location.pathname + location.search)) {
      document.getElementById("main")?.focus({ preventScroll: true });
    }
  }, [location]);
  return null;
}
