import type { ComponentType } from "react";
import { Navigate, Route, Routes, useParams } from "react-router-dom";
import { Layout } from "@/components/layout";
import { VaultShell } from "@/components/vault-shell";
import AdminPage from "@/pages/admin";
import AuthPage from "@/pages/auth";
import AuthForgotPage from "@/pages/auth-forgot";
import AuthCallbackPage from "@/pages/auth-callback";
import HomePage from "@/pages/home";
import VaultPage from "@/pages/vault";
import VaultIndexPage from "@/pages/vault-index";
import VaultNewPage from "@/pages/vault-new";
import DocumentPage from "@/pages/document";
import DocumentNewPage from "@/pages/document-new";
import TablePage from "@/pages/table";
import FilePage from "@/pages/file";
import GraphPage from "@/pages/graph";
import SearchPage from "@/pages/search";
import SettingsPage from "@/pages/settings";
import PublicationsPage from "@/pages/publications";
import PublicationPage from "@/pages/public-publication";
import VaultMembersPage from "@/pages/vault-members";
import VaultSettingsPage from "@/pages/vault-settings";
import VaultActivityPage from "@/pages/vault-activity";
import NotFoundPage from "@/pages/not-found";
import {
  appRouteContract,
  type AppRouteBoundary,
  type AppRouteComponentName,
} from "@/app-route-contract";

// Old /vault/:name/skill URLs redirect to the underlying guide document.
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

const routeComponents = {
  AdminPage,
  AuthPage,
  AuthForgotPage,
  AuthCallbackPage,
  PublicationPage,
  HomePage,
  VaultNewPage,
  VaultIndexPage,
  VaultPage,
  DocumentNewPage,
  DocumentPage,
  TablePage,
  FilePage,
  GraphPage,
  PublicationsPage,
  VaultMembersPage,
  VaultSettingsPage,
  VaultActivityPage,
  SearchPage,
  SkillRedirect,
  SettingsPage,
  NotFoundPage,
} satisfies Record<AppRouteComponentName, ComponentType>;

function renderRoutes(boundaries: readonly AppRouteBoundary[]) {
  return appRouteContract
    .filter((route) => boundaries.includes(route.boundary))
    .map((route) => {
      const Component = routeComponents[route.component];
      return <Route key={`${route.boundary}:${route.path}`} path={route.path} element={<Component />} />;
    });
}

/** The route tree shared by the production BrowserRouter and Storybook MemoryRouter. */
export function AppRoutes() {
  return (
    <Routes>
      {renderRoutes(["admin", "auth", "public"])}
      <Route element={<Layout />}>
        {renderRoutes(["app-layout"])}
        <Route element={<VaultShell />}>
          {renderRoutes(["vault-shell"])}
        </Route>
      </Route>
    </Routes>
  );
}
