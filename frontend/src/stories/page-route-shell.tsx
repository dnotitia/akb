import { AppRoutes } from "@/app-routes";

function resetWorkspaceChrome() {
  if (typeof window === "undefined") return;
  window.localStorage.setItem("akb.treeVisible", "1");
  window.localStorage.setItem("akb.vaultRailCollapsed", "0");
}

/** Storybook adapter for the canonical application route tree. */
export function AkbRouteTree() {
  resetWorkspaceChrome();
  return <AppRoutes />;
}
