import { useId, useLayoutEffect } from "react";
import { Button } from "@/components/ui/button";
import { Link, useLocation } from "react-router-dom";
import { getVaultPageActions } from "@/components/title-bar";
import { useResourceNavigation } from "@/contexts/resource-navigation-context";

/** Overview owns page discovery; breadcrumbs only describe the current location. */
export function VaultOverviewNavigation({ vault, memberCount }: { vault: string; memberCount?: number }) {
  const memberCountId = useId();
  const hasMemberCount = typeof memberCount === "number" && Number.isFinite(memberCount) && memberCount >= 0;
  const { requestNavigation } = useResourceNavigation();
  const actions = getVaultPageActions(vault).filter(action => action.key !== "overview" && action.key !== "search");
  const renderAction = (action: (typeof actions)[number]) => (
    <Button key={action.key} asChild variant="ghost" size="sm" className="vault-overview-page-link"><Link aria-label={action.key === "members" ? "Members" : undefined}
      aria-describedby={action.key === "members" && hasMemberCount ? memberCountId : undefined}
      to={action.href} state={{ vaultPageNavigation: action.href }}
      onClick={event => {
        if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.defaultPrevented) return;
        if (!requestNavigation(action.href)) event.preventDefault();
      }}>
      <action.icon className="h-3.5 w-3.5 shrink-0" aria-hidden />
      {action.key === "publish" ? "Publications" : action.label}
      {action.key === "members" && hasMemberCount && <span aria-hidden className="text-xs font-normal tabular-nums text-foreground-muted">{memberCount}</span>}
    </Link></Button>
  );
  return <nav aria-label="Vault pages" className="vault-overview-links">
    <div role="group" aria-label="Knowledge views" className="vault-overview-link-group">
      {actions.filter(action => action.key === "graph" || action.key === "publish").map(renderAction)}
    </div>
    <div role="group" aria-label="Vault administration" className="vault-overview-link-group">
      {actions.filter(action => action.key === "members" || action.key === "settings").map(renderAction)}
    </div>
    {hasMemberCount && <span id={memberCountId} className="sr-only">{memberCount} members</span>}
  </nav>;

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
