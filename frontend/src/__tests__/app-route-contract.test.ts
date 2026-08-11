import { describe, expect, it } from "vitest";
import { appRouteContract } from "@/app-route-contract";

describe("application route contract", () => {
  it("keeps route, component, and shell ownership explicit", () => {
    expect(appRouteContract).toEqual([
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
    ]);
  });

  it("does not declare duplicate paths", () => {
    const paths = appRouteContract.map(({ path }) => path);
    expect(new Set(paths).size).toBe(paths.length);
  });
});
