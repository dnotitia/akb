export type AppRouteBoundary = "auth" | "public" | "app-layout" | "vault-shell";

export interface AppRouteDefinition {
  path: string;
  component: string;
  boundary: AppRouteBoundary;
}

/**
 * Canonical route/component ownership for the AKB frontend.
 *
 * Both the browser application and Storybook's routed page scenarios render
 * from this contract. Keep the component name explicit: the contract test is
 * intentionally a review gate for route, page, and shell-owner changes.
 */
export const appRouteContract = [
  { path: "/auth", component: "AuthPage", boundary: "auth" },
  { path: "/auth/forgot", component: "AuthForgotPage", boundary: "auth" },
  { path: "/auth/callback", component: "AuthCallbackPage", boundary: "auth" },
  { path: "/p/:slug", component: "PublicationPage", boundary: "public" },
  { path: "/", component: "HomePage", boundary: "app-layout" },
  { path: "/vault/new", component: "VaultNewPage", boundary: "app-layout" },
  { path: "/vault", component: "VaultIndexPage", boundary: "vault-shell" },
  { path: "/vault/:name", component: "VaultPage", boundary: "vault-shell" },
  { path: "/vault/:name/doc/new", component: "DocumentNewPage", boundary: "vault-shell" },
  { path: "/vault/:name/doc/:id", component: "DocumentPage", boundary: "vault-shell" },
  { path: "/vault/:name/table/:table", component: "TablePage", boundary: "vault-shell" },
  { path: "/vault/:name/file/:id", component: "FilePage", boundary: "vault-shell" },
  { path: "/vault/:name/graph", component: "GraphPage", boundary: "vault-shell" },
  { path: "/vault/:name/publications", component: "PublicationsPage", boundary: "vault-shell" },
  { path: "/vault/:name/members", component: "VaultMembersPage", boundary: "vault-shell" },
  { path: "/vault/:name/settings", component: "VaultSettingsPage", boundary: "vault-shell" },
  { path: "/vault/:name/activity", component: "VaultActivityPage", boundary: "vault-shell" },
  { path: "/vault/:name/search", component: "SearchPage", boundary: "vault-shell" },
  { path: "/vault/:name/skill", component: "SkillRedirect", boundary: "vault-shell" },
  { path: "/search", component: "SearchPage", boundary: "app-layout" },
  { path: "/settings", component: "SettingsPage", boundary: "app-layout" },
  { path: "*", component: "NotFoundPage", boundary: "app-layout" },
] as const satisfies readonly AppRouteDefinition[];

export type AppRouteComponentName = (typeof appRouteContract)[number]["component"];
