import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BrowserRouter, Outlet } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { VaultRefreshProvider } from "@/contexts/vault-refresh-context";
import { installDocumentHistoryGuard } from "@/hooks/use-document-history-guard";
import { vaultGuideEditHref, vaultGuideHref } from "@/lib/vault-guide-navigation";

vi.mock("@/components/vault-shell", () => ({ VaultShell: () => <Outlet /> }));
vi.mock("@/components/app-sidebar", () => ({ AppSidebar: () => null }));
vi.mock("@/components/app-page-location", () => ({ AppPageLocation: () => null }));
vi.mock("@/components/global-search-dialog", () => ({ GlobalSearchDialog: () => null }));
vi.mock("@/components/header-indexing-status", () => ({ HeaderIndexingStatus: () => null }));
vi.mock("@/components/notification-bell", () => ({ NotificationBell: () => null }));
vi.mock("@/components/user-menu", () => ({ UserMenu: () => null }));
vi.mock("@/lib/api", async original => ({
  ...await original<typeof import("@/lib/api")>(),
  getDocument: vi.fn(), getVaultInfo: vi.fn(), updateDocument: vi.fn(),
  getMe: vi.fn(), getAuthConfig: vi.fn().mockResolvedValue({ available: true, auth_mode: "sso" }),
  getDocumentHistoryWithFallback: vi.fn().mockResolvedValue({ kind: "document_history", source: "document", history: [] }),
  getRelations: vi.fn().mockResolvedValue({ relations: [] }),
  authenticatedFetch: vi.fn().mockResolvedValue({ ok: false }),
}));

import { AppRoutes } from "@/app-routes";
import { getDocument, getMe, getVaultInfo, updateDocument } from "@/lib/api";

const DOC = { path: "overview/vault-skill.md", title: "Vault guide", content: "# Instructions\n\nSaved guide body", current_commit: "original-commit", type: "skill", tags: [] };
const USER = { user_id: "owner", username: "owner", email: "owner@example.test", display_name: null, is_admin: false, auth_method: "local", key_class: null };
let stopHistory: (() => void) | undefined;

function renderAt(url: string) {
  window.history.replaceState(null, "", url);
  stopHistory = installDocumentHistoryGuard();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><BrowserRouter>
    <CurrentUserProvider user={USER}><VaultRefreshProvider refetchVaults={vi.fn()} refetchTree={vi.fn()}>
      <AppRoutes />
    </VaultRefreshProvider></CurrentUserProvider>
  </BrowserRouter></QueryClientProvider>);
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getMe).mockResolvedValue(USER);
  vi.mocked(getVaultInfo).mockResolvedValue({ name: "v", role: "owner", description: "Saved description", public_access: "none" });
  vi.mocked(getDocument).mockResolvedValue(DOC);
  vi.mocked(updateDocument).mockResolvedValue({ current_commit: "saved-commit" });
});
afterEach(async () => {
  cleanup();
  stopHistory?.();
  window.history.replaceState(null, "", "/");
  // The real guide preview uses Tiptap, which defers destruction by one tick
  // to support StrictMode remounts. Finish it before jsdom removes window.
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 1)); });
});

describe("one read-first Vault guide flow", () => {
  it.each([vaultGuideHref("v"), "/vault/v/skill", `${vaultGuideHref("v")}?view=edit`, "/vault/v/doc/document-uuid?view=edit"])("opens %s in the reader and enters only the Settings editor", async url => {
    const user = userEvent.setup();
    renderAt(url);
    const edit = await screen.findByRole("link", { name: "Edit guide" });
    expect(edit).toHaveAttribute("href", vaultGuideEditHref("v"));
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Document title" })).not.toBeInTheDocument();
    await user.click(edit);
    const editor = await screen.findByRole("textbox", { name: "Vault guide body" });
    expect(editor).toHaveValue(DOC.content);
    await waitFor(() => expect(editor).toHaveFocus());
    expect(window.location.pathname + window.location.search + window.location.hash).toBe("/vault/v/settings#skill");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByLabelText("Vault guide body")).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("button", { name: "Edit guide" })).toHaveFocus());
    expect(updateDocument).not.toHaveBeenCalled();
  });

  it.each(["reader", "writer", "admin", "archived"])("keeps the guide readable without a second editor for %s", async role => {
    vi.mocked(getVaultInfo).mockResolvedValue({ role: role === "archived" ? "owner" : role, is_archived: role === "archived" });
    renderAt(`${vaultGuideHref("v")}?view=edit`);
    expect(await screen.findByRole("heading", { level: 1, name: "Vault guide" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Edit guide" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(document.querySelector('[contenteditable="true"]')).toBeNull();
    expect(screen.queryByLabelText("Vault guide body")).not.toBeInTheDocument();
  });

  it("preserves all Settings drafts and the original guide revision through preview, Back and Forward", async () => {
    const user = userEvent.setup();
    renderAt(vaultGuideEditHref("v"));
    const editor = await screen.findByLabelText("Vault guide body");
    fireEvent.change(editor, { target: { value: "Unsaved instructions" } });
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: "Unsaved description" } });
    await user.click(screen.getByRole("radio", { name: "Everyone can view" }));
    const previewLink = screen.getByRole("link", { name: "Open guide" });
    await user.click(previewLink);
    const preview = await screen.findByRole("dialog", { name: "Document preview" });
    expect(within(preview).getByRole("link", { name: "Edit guide" })).toBeVisible();
    expect(within(preview).queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: /Discard unsaved/ })).not.toBeInTheDocument();
    await user.click(within(preview).getByRole("radio", { name: "Raw markdown" }));
    await act(async () => window.history.back());
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Document preview" })).not.toBeInTheDocument());
    expect(screen.getByLabelText("Vault guide body")).toHaveValue("Unsaved instructions");
    expect(screen.getByLabelText("Description")).toHaveValue("Unsaved description");
    expect(screen.getByRole("radio", { name: "Everyone can view" })).toBeChecked();
    await waitFor(() => expect(previewLink).toHaveFocus());
    await act(async () => window.history.forward());
    await screen.findByRole("dialog", { name: "Document preview" });
    await user.keyboard("{Escape}");
    await waitFor(() => expect(previewLink).toHaveFocus());
    vi.mocked(getDocument).mockResolvedValue({ ...DOC, content: "Remote instructions", current_commit: "remote-commit" });
    await user.click(screen.getByRole("button", { name: "Save guide" }));
    await waitFor(() => expect(updateDocument).toHaveBeenCalledWith("v", DOC.path, { content: "Unsaved instructions", expected_commit: "original-commit" }));
  });

  it("guards promotion of the preview because that would unmount Settings drafts", async () => {
    const user = userEvent.setup();
    renderAt(vaultGuideEditHref("v"));
    fireEvent.change(await screen.findByLabelText("Vault guide body"), { target: { value: "Unsaved instructions" } });
    await user.click(screen.getByRole("link", { name: "Open guide" }));
    const preview = await screen.findByRole("dialog", { name: "Document preview" });
    await user.click(within(preview).getByRole("button", { name: "Open document in vault" }));
    const confirmation = await screen.findByRole("dialog", { name: "Discard unsaved settings changes?" });
    await user.click(within(confirmation).getByRole("button", { name: "Keep editing" }));
    expect(screen.getByRole("dialog", { name: "Document preview" })).toBeVisible();
    await user.keyboard("{Escape}");
    expect(await screen.findByLabelText("Vault guide body")).toHaveValue("Unsaved instructions");
  });

  it("resumes the existing Settings draft when Edit guide is selected from its preview", async () => {
    const user = userEvent.setup();
    renderAt(vaultGuideEditHref("v"));
    fireEvent.change(await screen.findByLabelText("Vault guide body"), { target: { value: "Unsaved instructions" } });
    await user.click(screen.getByRole("link", { name: "Open guide" }));
    const preview = await screen.findByRole("dialog", { name: "Document preview" });
    await user.click(within(preview).getByRole("link", { name: "Edit guide" }));
    const editor = await screen.findByLabelText("Vault guide body");
    expect(editor).toHaveValue("Unsaved instructions");
    await waitFor(() => expect(editor).toHaveFocus());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save guide" }));
    await waitFor(() => expect(updateDocument).toHaveBeenCalledWith("v", DOC.path, { content: "Unsaved instructions", expected_commit: "original-commit" }));
  });
});
