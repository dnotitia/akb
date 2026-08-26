import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import VaultSettingsPage from "@/pages/vault-settings";

vi.mock("@/lib/api", () => ({
  archiveVault: vi.fn(),
  authenticatedFetch: vi.fn().mockResolvedValue({ ok: false }),
  deleteVaultPermanent: vi.fn(),
  getDocument: vi.fn().mockRejectedValue(new Error("missing guide")),
  getSkillTemplate: vi.fn(),
  getVaultInfo: vi.fn(),
  getVaultSkillPreview: vi.fn(),
  unarchiveVault: vi.fn(),
  updateDocument: vi.fn(),
  updateVault: vi.fn(),
}));

import { getVaultInfo, updateVault } from "@/lib/api";

const getVaultInfoMock = getVaultInfo as unknown as ReturnType<typeof vi.fn>;
const updateVaultMock = updateVault as unknown as ReturnType<typeof vi.fn>;

const VAULT_INFO = {
  name: "platform-docs",
  description: "Product and platform knowledge.",
  role: "owner",
  public_access: "none",
  owner_display_name: "Vault Owner",
  created_at: "2026-08-20T10:00:00Z",
  last_activity: "2026-08-24T10:00:00Z",
  member_count: 4,
  collection_count: 3,
  document_count: 18,
  table_count: 2,
  file_count: 5,
  edge_count: 9,
};

function renderSettings() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/vault/platform-docs/settings"]}>
        <Routes>
          <Route path="/vault/:name/settings" element={<VaultSettingsPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  getVaultInfoMock.mockResolvedValue(VAULT_INFO);
  updateVaultMock.mockResolvedValue({ ok: true });
});

afterEach(cleanup);

describe("Vault Settings redesign", () => {
  it("keeps governance controls connected to the current vault context", async () => {
    renderSettings();

    expect(
      await screen.findByRole("heading", { level: 1, name: "Settings" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "General" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Access" })).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Vault guide" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Danger zone" }),
    ).toBeInTheDocument();

    for (const title of ["General", "Access", "Vault guide"]) {
      const header = screen
        .getByRole("heading", { name: title })
        .closest('[data-slot="settings-section-header"]');
      expect(header).toHaveClass("bg-surface-2/55", "border-border-strong");
    }

    const overviewLinks = screen.getAllByRole("link", { name: /overview/i });
    expect(
      overviewLinks.every(
        (link) => link.getAttribute("href") === "/vault/platform-docs",
      ),
    ).toBe(true);

    const context = screen.getByRole("complementary", {
      name: "Vault settings context",
    });
    expect(context).toHaveTextContent("akb://platform-docs");
    expect(context).toHaveTextContent("Vault Owner");
    expect(context).toHaveTextContent("Documents");
    expect(context).toHaveTextContent("18");
    expect(
      context.querySelector('[data-slot="vault-context-header"]'),
    ).toHaveClass("bg-surface-2/55", "border-border-strong");
    expect(
      screen.getByText("About").closest('[data-slot="panel-header"]'),
    ).toHaveClass("bg-surface-2/55", "border-border-strong");

    const sectionNav = screen.getByRole("navigation", {
      name: "Vault settings sections",
    });
    const settingsLayout = sectionNav.parentElement;
    const settingsMain = settingsLayout?.querySelector("main");
    expect(screen.getByTestId("settings-workspace-shell")).not.toHaveClass(
      "xl:p-4",
      "2xl:p-5",
    );
    expect(screen.getByTestId("settings-workspace-frame")).toHaveClass(
      "xl:overflow-hidden",
    );
    expect(screen.getByTestId("settings-workspace-frame")).not.toHaveClass(
      "xl:rounded-[var(--radius-md)]",
      "xl:border",
    );
    expect(settingsLayout).toHaveClass("min-h-full", "w-full");
    expect(settingsLayout).not.toHaveClass("max-w-[1680px]");
    expect(sectionNav).toHaveClass("lg:col-start-1", "lg:row-start-1");
    expect(settingsMain).toHaveClass("lg:col-start-2", "lg:row-start-1");
    expect(settingsMain).toHaveClass("xl:overflow-y-auto", "rail-scroll");
    expect(context).toHaveClass(
      "xl:col-start-3",
      "xl:row-start-1",
      "xl:overflow-y-auto",
      "xl:p-0",
      "rail-scroll",
    );
  });

  it("connects the read-only policy notice directly to the settings workspace", async () => {
    getVaultInfoMock.mockResolvedValue({
      ...VAULT_INFO,
      role: "reader",
      role_source: "member",
    });
    renderSettings();

    const message = await screen.findByText(
      "You can review these settings, but only the vault owner can change them.",
    );
    const notice = message.closest('[role="status"]');
    expect(notice).not.toBeNull();
    expect(notice).toHaveClass(
      "rounded-none",
      "border-x-0",
      "border-t-0",
      "border-b",
      "shrink-0",
    );
    expect(notice).not.toHaveClass("mb-5");

    const sectionNav = screen.getByRole("navigation", {
      name: "Vault settings sections",
    });
    expect(notice?.nextElementSibling).toBe(sectionNav.parentElement);
    expect(notice?.parentElement).toBe(
      screen.getByTestId("settings-workspace-frame"),
    );
  });

  it("saves General and Access as independent settings", async () => {
    const user = userEvent.setup();
    renderSettings();

    const description = await screen.findByLabelText("Description");
    await user.clear(description);
    await user.type(description, "A concise operating knowledge base.");
    await user.click(
      screen.getAllByRole("button", { name: "Save changes" })[0],
    );

    await waitFor(() =>
      expect(updateVaultMock).toHaveBeenCalledWith("platform-docs", {
        description: "A concise operating knowledge base.",
      }),
    );

    await user.click(screen.getByRole("radio", { name: "Public · read" }));
    await user.click(
      screen.getAllByRole("button", { name: "Save changes" })[1],
    );

    await waitFor(() =>
      expect(updateVaultMock).toHaveBeenCalledWith("platform-docs", {
        public_access: "reader",
      }),
    );
  });
});
