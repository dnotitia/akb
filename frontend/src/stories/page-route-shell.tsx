import { Navigate, Route, Routes, useParams } from "react-router-dom";
import { Layout } from "@/components/layout";
import { VaultShell } from "@/components/vault-shell";
import AuthPage from "@/pages/auth";
import AuthCallbackPage from "@/pages/auth-callback";
import AuthForgotPage from "@/pages/auth-forgot";
import DocumentNewPage from "@/pages/document-new";
import DocumentPage from "@/pages/document";
import FilePage from "@/pages/file";
import GraphPage from "@/pages/graph";
import HomePage from "@/pages/home";
import NotFoundPage from "@/pages/not-found";
import PublicationPage from "@/pages/public-publication";
import PublicationsPage from "@/pages/publications";
import SearchPage from "@/pages/search";
import SettingsPage from "@/pages/settings";
import TablePage from "@/pages/table";
import VaultActivityPage from "@/pages/vault-activity";
import VaultIndexPage from "@/pages/vault-index";
import VaultMembersPage from "@/pages/vault-members";
import VaultNewPage from "@/pages/vault-new";
import VaultPage from "@/pages/vault";
import VaultSettingsPage from "@/pages/vault-settings";

export const routeClassification = {
  publicRoutes: ["/p/:slug"],
  authRoutes: ["/auth", "/auth/forgot", "/auth/callback"],
  appLayoutRoutes: ["/", "/vault/new", "/search", "/settings", "*"],
  vaultShellRoutes: [
    "/vault",
    "/vault/:name",
    "/vault/:name/doc/new",
    "/vault/:name/doc/:id",
    "/vault/:name/table/:table",
    "/vault/:name/file/:id",
    "/vault/:name/graph",
    "/vault/:name/publications",
    "/vault/:name/members",
    "/vault/:name/settings",
    "/vault/:name/activity",
    "/vault/:name/search",
    "/vault/:name/skill",
  ],
} as const;

function SkillRedirect() {
  const { name } = useParams<{ name: string }>();
  if (!name) return <Navigate to="/" replace />;
  return (
    <Navigate
      to={`/vault/${name}/doc/${encodeURIComponent("overview/vault-skill.md")}`}
      replace
    />
  );
}

function resetWorkspaceChrome() {
  if (typeof window === "undefined") return;
  window.localStorage.setItem("akb.treeVisible", "1");
  window.localStorage.setItem("akb.vaultRailCollapsed", "0");
}

export function AkbRouteTree() {
  resetWorkspaceChrome();

  return (
    <Routes>
      <Route path="/auth" element={<AuthPage />} />
      <Route path="/auth/forgot" element={<AuthForgotPage />} />
      <Route path="/auth/callback" element={<AuthCallbackPage />} />
      <Route path="/p/:slug" element={<PublicationPage />} />
      <Route element={<Layout />}>
        <Route path="/" element={<HomePage />} />
        <Route path="/vault/new" element={<VaultNewPage />} />
        <Route element={<VaultShell />}>
          <Route path="/vault" element={<VaultIndexPage />} />
          <Route path="/vault/:name" element={<VaultPage />} />
          <Route path="/vault/:name/doc/new" element={<DocumentNewPage />} />
          <Route path="/vault/:name/doc/:id" element={<DocumentPage />} />
          <Route path="/vault/:name/table/:table" element={<TablePage />} />
          <Route path="/vault/:name/file/:id" element={<FilePage />} />
          <Route path="/vault/:name/graph" element={<GraphPage />} />
          <Route path="/vault/:name/publications" element={<PublicationsPage />} />
          <Route path="/vault/:name/members" element={<VaultMembersPage />} />
          <Route path="/vault/:name/settings" element={<VaultSettingsPage />} />
          <Route path="/vault/:name/activity" element={<VaultActivityPage />} />
          <Route path="/vault/:name/search" element={<SearchPage />} />
          <Route path="/vault/:name/skill" element={<SkillRedirect />} />
        </Route>
        <Route path="/search" element={<SearchPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
