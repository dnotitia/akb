import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import VaultIndexPage from "@/pages/vault-index";
import { CurrentUserProvider } from "@/contexts/current-user-context";

vi.mock("@/lib/api", () => ({
  listVaults: vi.fn(),
}));

import { listVaults } from "@/lib/api";

const listVaultsMock = listVaults as unknown as ReturnType<typeof vi.fn>;

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/vault"]}>
      <VaultIndexPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  listVaultsMock.mockReset();
});

afterEach(cleanup);

describe("vault index no-selection states", () => {
  it("clears the directory during proof and ignores a previous account's completion", async () => {
    let resolve!: (value: { vaults: Array<{ id: string; name: string }> }) => void;
    listVaultsMock.mockReturnValueOnce(new Promise(r => { resolve = r; })).mockResolvedValue({ vaults: [] });
    const view = (id: string, checking = false, revision = 0) => <MemoryRouter><CurrentUserProvider user={{ user_id: id, username: id, email: `${id}@x.test`, display_name: null, is_admin: false, auth_method: "local", key_class: null }} checking={checking} revision={revision}><VaultIndexPage /></CurrentUserProvider></MemoryRouter>;
    const { rerender } = render(view("a"));
    await waitFor(() => expect(listVaultsMock).toHaveBeenCalledOnce());
    rerender(view("b", true));
    await act(async () => resolve({ vaults: [{ id: "private-a", name: "private-a" }] }));
    expect(screen.queryByRole("link", { name: "private-a" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("1 vault available")).not.toBeInTheDocument();
    expect(listVaultsMock).toHaveBeenCalledOnce();
    rerender(view("b", false, 1));
    await screen.findByRole("heading", { name: "Create your first vault." });
    expect(listVaultsMock).toHaveBeenCalledTimes(2);
  });
  it("provides direct vault access even when the navigation sidebar is hidden", async () => {
    listVaultsMock.mockResolvedValue({
      vaults: [
        { id: "v-owned", name: "owned", role: "owner" },
        { id: "v-shared", name: "shared", role: "writer" },
      ],
    });
    renderPage();

    expect(
      await screen.findByRole("heading", {
        name: "Your vaults",
      }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("2 vaults available")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "owned" })).toHaveAttribute("href", "/vault/owned");
    expect(screen.getByRole("link", { name: "shared" })).toHaveAttribute("href", "/vault/shared");
    expect(screen.queryByRole("heading", { name: "Workspace route" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New vault" })).toBeEnabled();
  });

  it("turns a truly empty account into a focused first-vault action", async () => {
    listVaultsMock.mockResolvedValue({ vaults: [] });
    renderPage();

    expect(
      await screen.findByRole("heading", { name: "Create your first vault." }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create first vault" })).toBeEnabled();
    expect(screen.getByText(/ask a vault owner to invite you/i)).toBeInTheDocument();
  });

  it("surfaces a load failure and retries in place", async () => {
    const user = userEvent.setup();
    listVaultsMock
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce({ vaults: [{ id: "v-owned", name: "owned", role: "owner" }] });
    renderPage();

    expect(await screen.findByRole("alert")).toHaveTextContent("Vaults could not be loaded");
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(
      await screen.findByRole("heading", {
        name: "Your vaults",
      }),
    ).toBeInTheDocument();
    expect(listVaultsMock).toHaveBeenCalledTimes(2);
  });

  it("filters a large directory and progressively reveals remaining vaults", async () => {
    const user = userEvent.setup();
    listVaultsMock.mockResolvedValue({ vaults: Array.from({ length: 65 }, (_, index) => ({
      id: `v-${index}`, name: `workspace-${String(index).padStart(2, "0")}`, role: index % 2 ? "reader" : "owner",
    })) });
    renderPage();
    await screen.findByRole("heading", { name: "Your vaults" });
    expect(screen.getAllByRole("link")).toHaveLength(30);
    await user.click(screen.getByRole("button", { name: "Show more vaults" }));
    expect(screen.getAllByRole("link")).toHaveLength(60);
    await user.type(screen.getByRole("textbox", { name: "Find a vault" }), "workspace-64");
    expect(screen.getAllByRole("link")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Owned" }));
    expect(screen.getByRole("link", { name: "workspace-64" })).toBeInTheDocument();
    await user.clear(screen.getByRole("textbox", { name: "Find a vault" }));
    expect(screen.getAllByRole("link")).toHaveLength(30);
  });
});
