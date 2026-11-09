import { useEffect, useRef } from "react";
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
// The shared editor's parent owns permissions, drafts and payload construction.
// Rich-editor interactions are covered by the deployed browser suite.
vi.mock("@/components/markdown-editor", () => ({
  default: function GuideEditorMock({ value, onChange, ariaLabel, readOnly }: { value: string; onChange?: (body: string) => void; ariaLabel?: string; readOnly?: boolean }) {
    const initial = useRef({ value, onChange });
    useEffect(() => { initial.current.onChange?.(initial.current.value); }, []);
    return <textarea aria-label={ariaLabel} defaultValue={value} readOnly={readOnly} onChange={event => onChange?.(event.target.value)} />;
  },
}));
vi.mock("@/lib/api", async original => ({
  ...await original<typeof import("@/lib/api")>(),
  getDocument: vi.fn(), getVaultInfo: vi.fn(), updateDocument: vi.fn(),
  getMe: vi.fn(), getAuthConfig: vi.fn().mockResolvedValue({ available: true, auth_mode: "sso" }),
  getDocumentHistoryWithFallback: vi.fn().mockResolvedValue({ kind: "document_history", source: "document", history: [] }),
  getRelations: vi.fn().mockResolvedValue({ relations: [] }),
  authenticatedFetch: vi.fn().mockResolvedValue({ ok: false }),
}));

import { AppRoutes } from "@/app-routes";
import { ApiError, getDocument, getMe, getVaultInfo, updateDocument } from "@/lib/api";

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
  localStorage.clear();
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

describe("one Vault guide document workspace", () => {
  it.each([vaultGuideHref("v"), "/vault/v/skill", vaultGuideEditHref("v"), "/vault/v/doc/document-uuid?view=edit"])("edits only the body in the document workspace from %s", async url => {
    const user = userEvent.setup();
    renderAt(url);
    await waitFor(() => expect(window.location.pathname).toBe(vaultGuideHref("v")));
    if (!url.includes("view=edit")) await user.click(await screen.findByRole("button", { name: "Edit guide" }));
    const editor = await screen.findByRole("textbox", { name: "Vault guide body" });
    expect(editor).toHaveValue(DOC.content);
    expect(screen.queryByRole("textbox", { name: "Document title" })).not.toBeInTheDocument();
    expect(screen.queryByRole("complementary", { name: "Document details" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Type")).not.toBeInTheDocument();
    fireEvent.change(editor, { target: { value: "# Updated instructions" } });
    expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(updateDocument).toHaveBeenCalledWith("v", expect.any(String), {
      content: "# Updated instructions", expected_commit: "original-commit",
    }));
    expect(window.location.pathname).toContain("/doc/");
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Vault guide body" })).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Edit guide" })).toBeVisible();
  });

  it.each(["reader", "writer", "admin", "archived"])("keeps the guide read-only for %s", async role => {
    vi.mocked(getVaultInfo).mockResolvedValue({ role: role === "archived" ? "owner" : role, is_archived: role === "archived" });
    renderAt(vaultGuideEditHref("v"));
    expect(await screen.findByRole("heading", { level: 1, name: "Vault guide" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit guide" })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Vault guide body" })).not.toBeInTheDocument();
    expect(updateDocument).not.toHaveBeenCalled();
  });

  it.each(["Open guide", "Edit guide"])("%s retains Settings drafts in the same modal through reading, editing and closing", async entry => {
    const user = userEvent.setup();
    renderAt("/vault/v/settings#general");
    fireEvent.change(await screen.findByLabelText("Description"), { target: { value: "Unsaved description" } });
    await user.click(screen.getByRole("tab", { name: "Access permissions" }));
    const policy = screen.getByRole("radio", { name: "Everyone can view" });
    await user.click(policy);
    await user.click(screen.getByRole("tab", { name: "Vault guide" }));
    const trigger = await screen.findByRole(entry === "Open guide" ? "link" : "button", { name: entry });
    await user.click(trigger);
    const preview = await screen.findByRole("dialog", { name: "Document preview" });
    if (entry === "Open guide") await user.click(within(preview).getByRole("button", { name: "Edit guide" }));
    const editor = await within(preview).findByRole("textbox", { name: "Vault guide body" });
    expect(editor).toHaveValue(DOC.content);
    await user.click(within(preview).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(within(preview).queryByRole("textbox", { name: "Vault guide body" })).not.toBeInTheDocument());
    expect(preview).toBeVisible();
    expect(window.location.pathname).toBe(vaultGuideHref("v"));
    await user.click(within(preview).getByRole("button", { name: "Close dialog" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByLabelText("Description")).toHaveValue("Unsaved description");
    expect(policy).toBeChecked();
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("protects the modal editor draft on close and retains a failed save", async () => {
    const user = userEvent.setup();
    vi.mocked(updateDocument).mockRejectedValueOnce(new Error("Guide save failed"));
    renderAt("/vault/v/settings#skill");
    await user.click(await screen.findByRole("button", { name: "Edit guide" }));
    const preview = await screen.findByRole("dialog", { name: "Document preview" });
    const editor = await within(preview).findByRole("textbox", { name: "Vault guide body" });
    fireEvent.change(editor, { target: { value: "Unsaved instructions" } });
    await user.click(within(preview).getByRole("button", { name: "Save changes" }));
    expect(await within(preview).findByRole("alert")).toHaveTextContent("Guide save failed");
    await user.click(within(preview).getByRole("button", { name: "Close dialog" }));
    const confirmation = await screen.findByRole("dialog", { name: "Leave this document?" });
    await user.click(within(confirmation).getByRole("button", { name: "Keep editing" }));
    expect(editor).toHaveValue("Unsaved instructions");
    expect(preview).toBeVisible();
    expect(updateDocument).toHaveBeenCalledWith("v", DOC.path, { content: "Unsaved instructions", expected_commit: "original-commit" });
  });

  it("opens the legacy Settings edit deep link in the same document modal", async () => {
    renderAt("/vault/v/settings?edit=guide#skill");
    const preview = await screen.findByRole("dialog", { name: "Document preview" });
    expect(await within(preview).findByRole("textbox", { name: "Vault guide body" })).toHaveValue(DOC.content);
    expect(window.location.pathname + window.location.search).toBe(vaultGuideEditHref("v"));
  });

  it("preserves the Guide body and original revision after a conflict until explicitly rebased", async () => {
    const user = userEvent.setup();
    vi.mocked(updateDocument).mockRejectedValueOnce(new ApiError("revision moved", 409, { code: "conflict" }));
    renderAt(vaultGuideEditHref("v"));
    const editor = await screen.findByRole("textbox", { name: "Vault guide body" });
    fireEvent.change(editor, { target: { value: "Local guide instructions" } });
    vi.mocked(getDocument).mockResolvedValue({ ...DOC, content: "Other owner's changes", current_commit: "new-server-commit" });
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText("This document changed on the server")).toBeVisible();
    expect(editor).toHaveValue("Local guide instructions");
    expect(updateDocument).toHaveBeenCalledExactlyOnceWith("v", DOC.path, {
      content: "Local guide instructions", expected_commit: "original-commit",
    });
    await user.click(screen.getByRole("button", { name: "Apply draft to latest" }));
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(updateDocument).toHaveBeenLastCalledWith("v", DOC.path, {
      content: "Local guide instructions", expected_commit: "new-server-commit",
    }));
  });
});
