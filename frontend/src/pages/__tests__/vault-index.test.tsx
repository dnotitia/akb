import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import VaultIndexPage from "@/pages/vault-index";

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
  it("guides an existing user through the workspace without duplicating the directory", async () => {
    listVaultsMock.mockResolvedValue({
      vaults: [
        { id: "v-owned", name: "owned", role: "owner" },
        { id: "v-shared", name: "shared", role: "writer" },
      ],
    });
    renderPage();

    expect(
      await screen.findByRole("heading", {
        name: "Open a vault. Everything else follows.",
      }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("2 vaults available")).toBeInTheDocument();
    expect(screen.getByText("Owned")).toBeInTheDocument();
    expect(screen.getByText("Shared")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Workspace route" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New vault" })).toBeEnabled();
  });

  it("turns a truly empty account into a focused first-vault action", async () => {
    listVaultsMock.mockResolvedValue({ vaults: [] });
    renderPage();

    expect(
      await screen.findByRole("heading", { name: "Create your first vault." }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create first vault" })).toBeEnabled();
    expect(screen.getByRole("heading", { name: "Every knowledge shape, together" })).toBeInTheDocument();
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
        name: "Open a vault. Everything else follows.",
      }),
    ).toBeInTheDocument();
    expect(listVaultsMock).toHaveBeenCalledTimes(2);
  });
});
