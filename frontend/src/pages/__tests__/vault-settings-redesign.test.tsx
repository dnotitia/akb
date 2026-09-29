import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
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

import {
  archiveVault,
  deleteVaultPermanent,
  getDocument,
  getVaultInfo,
  unarchiveVault,
  updateDocument,
  updateVault,
} from "@/lib/api";

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

function BackButton() {
  const navigate = useNavigate();
  return <><button onClick={() => navigate(-1)}>Browser back</button>
    <button onClick={() => navigate("/vault")}>Leave settings</button>
    <button onClick={() => navigate("/vault/another/settings")}>Another vault</button></>;
}

function renderSettings(hash = "") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/vault/platform-docs/settings${hash}`]}>
        <BackButton />
        <Routes>
          <Route path="/vault/:name/settings" element={<VaultSettingsPage />} />
          <Route path="/vault" element={<h1>Vault directory</h1>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  getVaultInfoMock.mockResolvedValue(VAULT_INFO);
  updateVaultMock.mockResolvedValue({ ok: true });
  vi.mocked(getDocument).mockResolvedValue({
    content: "Original guide",
    current_commit: "original-commit",
  });
});

afterEach(cleanup);

describe("Vault Settings redesign", () => {
  it.each(["Leave settings", "Another vault"])("ignores a delayed save failure after %s", async (destination) => {
    const user = userEvent.setup();
    let rejectSave!: (error: Error) => void;
    updateVaultMock.mockReturnValueOnce(new Promise((_resolve, reject) => { rejectSave = reject; }));
    renderSettings("#access");
    await user.click(await screen.findByRole("radio", { name: "Public · read" }));
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await user.click(screen.getByRole("tab", { name: "General" }));
    await user.click(screen.getByRole("button", { name: destination }));
    if (destination === "Another vault") await screen.findByRole("textbox", { name: "Description" });
    await act(async () => { rejectSave(new Error("Old vault save failed")); });
    if (destination === "Leave settings") expect(screen.getByRole("heading", { name: "Vault directory" })).toBeVisible();
    else expect(screen.getByRole("tab", { name: "General", selected: true })).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps Vault details visible without disclosure across settings categories", async () => {
    const user = userEvent.setup();
    renderSettings();
    const details = await screen.findByRole("complementary", { name: "Vault details" });
    expect(within(details).getByText("Vault Owner")).toBeVisible();
    expect(within(details).getByText("Documents").nextElementSibling).toHaveTextContent("18");
    expect(within(details).getByRole("link", { name: "Open Vault Overview" })).toHaveAttribute("href", "/vault/platform-docs");
    for (const category of ["Access", "Vault guide", "Advanced", "General"]) {
      await user.click(screen.getByRole("tab", { name: category }));
      expect(details).toBeVisible();
      expect(screen.getAllByRole("complementary", { name: "Vault details" })).toHaveLength(1);
    }
  });

  it("records one navigation when pointer and focus select the same tab before rerender", async () => {
    const user = userEvent.setup();
    renderSettings();
    await screen.findByRole("textbox", { name: "Description" });
    const accessTab = screen.getByRole("tab", { name: "Access" });
    act(() => {
      fireEvent.mouseDown(accessTab, { button: 0, ctrlKey: false });
      fireEvent.focus(accessTab);
    });
    expect(accessTab).toHaveAttribute("aria-selected", "true");
    await user.click(screen.getByRole("button", { name: "Browser back" }));
    expect(screen.getByRole("tab", { name: "General" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("shows one section and restores the selected section with browser Back", async () => {
    const user = userEvent.setup();
    renderSettings("#access");
    expect(
      await screen.findByRole("tab", { name: "Access", selected: true }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("tabpanel")).toHaveLength(1);
    expect(
      screen.queryByRole("textbox", { name: "Description" }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "General" }));
    expect(screen.getByRole("textbox", { name: "Description" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Browser back" }));
    expect(
      screen.getByRole("tab", { name: "Access", selected: true }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("complementary", { name: "Vault settings context" }),
    ).not.toBeInTheDocument();
  });

  it.each(["reader", "admin"])(
    "keeps %s settings read-only in every category",
    async (role) => {
      const user = userEvent.setup();
      getVaultInfoMock.mockResolvedValue({
        ...VAULT_INFO,
        role,
        role_source: "member",
      });
      renderSettings();

      const message = await screen.findByText(
        "You can review these settings, but only the vault owner can change them.",
      );
      expect(message).toBeVisible();
      expect(
        screen.getByRole("tablist", { name: "Vault settings sections" }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("textbox", { name: "Description" }),
      ).toHaveAttribute("readonly");
      expect(
        screen.queryByRole("button", { name: "Save changes" }),
      ).not.toBeInTheDocument();
      await user.click(screen.getByRole("tab", { name: "Access" }));
      expect(screen.getByRole("radio", { name: "Private" })).toBeDisabled();
      await user.click(screen.getByRole("tab", { name: "Vault guide" }));
      expect(
        screen.queryByRole("tab", { name: "Edit" }),
      ).not.toBeInTheDocument();
      await user.click(screen.getByRole("tab", { name: "Advanced" }));
      expect(
        screen.queryByRole("button", { name: "Delete vault" }),
      ).not.toBeInTheDocument();
    },
  );

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

    await user.click(screen.getByRole("tab", { name: "Access" }));
    await user.click(screen.getByRole("radio", { name: "Public · read" }));
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() =>
      expect(updateVaultMock).toHaveBeenCalledWith("platform-docs", {
        public_access: "reader",
      }),
    );
  });

  it("retains description and guide drafts when switching categories", async () => {
    const user = userEvent.setup();
    renderSettings();
    const description = await screen.findByRole("textbox", {
      name: "Description",
    });
    await user.clear(description);
    await user.type(description, "Unsaved description");
    await user.click(screen.getByRole("tab", { name: "Vault guide" }));
    await user.click(await screen.findByRole("tab", { name: "Edit" }));
    await user.type(
      screen.getByRole("textbox", { name: "Vault guide body" }),
      " with edits",
    );
    await user.click(screen.getByRole("tab", { name: "General" }));
    expect(screen.getByRole("textbox", { name: "Description" })).toHaveValue(
      "Unsaved description",
    );
    await user.click(screen.getByRole("tab", { name: "Vault guide" }));
    expect(
      screen.getByRole("textbox", { name: "Vault guide body" }),
    ).toHaveValue("Original guide with edits");
    await user.click(screen.getByRole("button", { name: "Save guide" }));
    await waitFor(() =>
      expect(updateDocument).toHaveBeenCalledWith(
        "platform-docs",
        "overview/vault-skill.md",
        {
          content: "Original guide with edits",
          expected_commit: "original-commit",
        },
      ),
    );
  });

  it("requires confirmation before saving world-writable access", async () => {
    const user = userEvent.setup();
    renderSettings("#access");
    await user.click(
      await screen.findByRole("radio", { name: "Public · write" }),
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(updateVault).not.toHaveBeenCalled();
    await user.click(
      screen.getByRole("button", { name: "Make world-writable" }),
    );
    await waitFor(() =>
      expect(updateVault).toHaveBeenCalledWith("platform-docs", {
        public_access: "writer",
      }),
    );
  });

  it("retains a failed edit and its scoped error until discard", async () => {
    const user = userEvent.setup();
    updateVaultMock.mockRejectedValueOnce(
      new Error("Permission changed on server"),
    );
    renderSettings();
    await user.type(
      await screen.findByRole("textbox", { name: "Description" }),
      " changed",
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Permission changed on server",
    );
    await user.click(screen.getByRole("tab", { name: "Access" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "General" }));
    expect(screen.getByRole("textbox", { name: "Description" })).toHaveValue(
      "Product and platform knowledge. changed",
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Permission changed on server",
    );
    await user.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.getByRole("textbox", { name: "Description" })).toHaveValue(
      VAULT_INFO.description,
    );
  });

  it("returns to Access and focuses a failed save after changing sections", async () => {
    const user = userEvent.setup();
    let rejectSave!: (error: Error) => void;
    updateVaultMock.mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        rejectSave = reject;
      }),
    );
    renderSettings("#access");
    await user.click(
      await screen.findByRole("radio", { name: "Public · read" }),
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await user.click(screen.getByRole("tab", { name: "General" }));
    await act(async () => {
      rejectSave(new Error("Access save failed"));
    });
    expect(
      screen.getByRole("tab", { name: "Access", selected: true }),
    ).toBeInTheDocument();
    const error = screen.getByRole("alert");
    expect(error).toHaveTextContent("Access save failed");
    expect(error.parentElement).toHaveFocus();
  });

  it.each(["general", "access"])(
    "keeps focus on the current tab when a hidden %s save succeeds",
    async (scope) => {
      const user = userEvent.setup();
      let finishSave!: (value: { ok: boolean }) => void;
      updateVaultMock.mockReturnValueOnce(
        new Promise((resolve) => {
          finishSave = resolve;
        }),
      );
      renderSettings(`#${scope}`);
      if (scope === "general") {
        await user.type(
          await screen.findByRole("textbox", { name: "Description" }),
          " changed",
        );
      } else {
        await user.click(
          await screen.findByRole("radio", { name: "Public · read" }),
        );
      }
      await user.click(screen.getByRole("button", { name: "Save changes" }));
      const guideTab = screen.getByRole("tab", { name: "Vault guide" });
      await user.click(guideTab);
      expect(guideTab).toHaveFocus();
      await act(async () => {
        finishSave({ ok: true });
        await new Promise((resolve) => requestAnimationFrame(resolve));
      });
      expect(screen.getByText("Saved")).toBeInTheDocument();
      expect(guideTab).toHaveAttribute("aria-selected", "true");
      expect(guideTab).toHaveFocus();
    },
  );

  it("maps the legacy danger link to Advanced", async () => {
    renderSettings("#danger");
    expect(
      await screen.findByRole("tab", { name: "Advanced", selected: true }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Archive" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Delete vault" })).toBeVisible();
    expect(
      screen.getByText("Search-update status has not been verified."),
    ).toBeVisible();
  });

  it("keeps archive and restore behind their existing confirmations", async () => {
    const user = userEvent.setup();
    renderSettings("#danger");
    await user.click(await screen.findByRole("button", { name: "Archive" }));
    expect(archiveVault).not.toHaveBeenCalled();
    getVaultInfoMock.mockResolvedValue({ ...VAULT_INFO, is_archived: true });
    await user.click(screen.getByRole("button", { name: "Archive vault" }));
    await waitFor(() =>
      expect(archiveVault).toHaveBeenCalledWith("platform-docs"),
    );
    await user.click(await screen.findByRole("button", { name: "Unarchive" }));
    expect(unarchiveVault).not.toHaveBeenCalled();
    getVaultInfoMock.mockResolvedValue(VAULT_INFO);
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: "Unarchive",
      }),
    );
    await waitFor(() =>
      expect(unarchiveVault).toHaveBeenCalledWith("platform-docs"),
    );
    expect(
      await screen.findByRole("button", { name: "Archive" }),
    ).toBeVisible();
  });

  it("requires the exact name before deleting and returns to the directory", async () => {
    const user = userEvent.setup();
    renderSettings("#danger");
    await user.click(
      await screen.findByRole("button", { name: "Delete vault" }),
    );
    const dialog = screen.getByRole("dialog");
    expect(
      within(dialog).getByRole("button", { name: "Delete forever" }),
    ).toBeDisabled();
    await user.type(within(dialog).getByRole("textbox"), "platform-docs");
    await user.click(
      within(dialog).getByRole("button", { name: "Delete forever" }),
    );
    await waitFor(() =>
      expect(deleteVaultPermanent).toHaveBeenCalledWith("platform-docs"),
    );
    expect(
      await screen.findByRole("heading", { name: "Vault directory" }),
    ).toBeInTheDocument();
  });

  it("falls back for unknown sections and keeps unavailable details distinct from zero", async () => {
    getVaultInfoMock.mockResolvedValue({
      name: "platform-docs",
      role: "owner",
      document_count: null,
    });
    renderSettings("#unknown");
    expect(
      await screen.findByRole("tab", { name: "General", selected: true }),
    ).toBeInTheDocument();
    const details = await screen.findByRole("complementary", { name: "Vault details" });
    expect(details).toHaveTextContent("Not available");
    expect(
      within(details).getByText("Documents").nextElementSibling,
    ).toHaveTextContent("Not available");
    expect(within(details).queryByText("0")).not.toBeInTheDocument();
  });

  it("recovers a failed initial load without losing the requested category", async () => {
    const user = userEvent.setup();
    getVaultInfoMock.mockRejectedValueOnce(new Error("Vault unavailable"));
    renderSettings("#access");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Vault unavailable",
    );
    expect(
      screen.getByRole("tab", { name: "Access", selected: true }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("radio", { name: "Private" })).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("opens the guide deep link without enabling archived guide edits", async () => {
    getVaultInfoMock.mockResolvedValue({ ...VAULT_INFO, is_archived: true });
    renderSettings("#skill");
    expect(
      await screen.findByRole("tab", { name: "Vault guide", selected: true }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("tab", { name: "Agent view" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Edit" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Reset to template" }),
    ).not.toBeInTheDocument();
  });
});
