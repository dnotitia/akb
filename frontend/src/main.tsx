import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Routes, Route, Navigate, useParams } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Layout } from "@/components/layout";
import { VaultShell } from "@/components/vault-shell";
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
import "./index.css";

// Vite dispatches `vite:preloadError` on window when a dynamically-imported
// chunk fails to load — the classic stale-deploy symptom: a client holding
// an old index.html requests a chunk hash a newer build has removed, so the
// fetch 404s (or, with the old nginx config, returned index.html and failed
// to parse as a module). Recover by reloading once onto the fresh build.
// (The event type ships with vite/client.)
const PRELOAD_RELOAD_AT = "akb.preloadReloadAt";
window.addEventListener("vite:preloadError", (event) => {
  // Loop guard: if we already reloaded for this in the last 10s, the chunk
  // is genuinely broken (not merely stale) — let the error surface to the
  // ErrorBoundary instead of reloading forever.
  const last = Number(sessionStorage.getItem(PRELOAD_RELOAD_AT) || 0);
  if (Date.now() - last < 10_000) return;
  sessionStorage.setItem(PRELOAD_RELOAD_AT, String(Date.now()));
  event.preventDefault(); // we're handling it — suppress the default throw
  window.location.reload();
});

// Old /vault/:name/skill URLs now redirect to the underlying guide doc;
// removed the dedicated page because it duplicated DocumentPage.
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

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 30_000, retry: 1, refetchOnWindowFocus: false },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
    <BrowserRouter>
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
          {/* Catch-all: an unmatched URL used to render a blank page with no
              recovery. Render NotFound inside the shell instead. */}
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>
);
