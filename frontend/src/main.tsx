import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import { Layout } from "@/components/layout";
import { VaultShell } from "@/components/vault-shell";
import AuthPage from "@/pages/auth";
import AuthForgotPage from "@/pages/auth-forgot";
import HomePage from "@/pages/home";
import VaultPage from "@/pages/vault";
import VaultIndexPage from "@/pages/vault-index";
import VaultNewPage from "@/pages/vault-new";
import DocumentPage from "@/pages/document";
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
import "./index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/auth" element={<AuthPage />} />
        <Route path="/auth/forgot" element={<AuthForgotPage />} />
        <Route path="/p/:slug" element={<PublicationPage />} />
        <Route element={<Layout />}>
          <Route path="/" element={<HomePage />} />
          <Route path="/vault/new" element={<VaultNewPage />} />
          <Route element={<VaultShell />}>
            <Route path="/vault" element={<VaultIndexPage />} />
            <Route path="/vault/:name" element={<VaultPage />} />
            <Route path="/vault/:name/doc/:id" element={<DocumentPage />} />
            <Route path="/vault/:name/table/:table" element={<TablePage />} />
            <Route path="/vault/:name/file/:id" element={<FilePage />} />
            <Route path="/vault/:name/graph" element={<GraphPage />} />
            <Route path="/vault/:name/publications" element={<PublicationsPage />} />
            <Route path="/vault/:name/members" element={<VaultMembersPage />} />
            <Route path="/vault/:name/settings" element={<VaultSettingsPage />} />
            <Route path="/vault/:name/activity" element={<VaultActivityPage />} />
            <Route path="/vault/:name/search" element={<SearchPage />} />
          </Route>
          <Route path="/search" element={<SearchPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  </StrictMode>
);
