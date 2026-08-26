import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import VaultActivityPage from "@/pages/vault-activity";

vi.mock("@/lib/api", () => ({
  getVaultActivity: vi.fn(),
}));

import { getVaultActivity } from "@/lib/api";

const getVaultActivityMock = getVaultActivity as unknown as ReturnType<
  typeof vi.fn
>;

function activity(index: number, actor = "JY Kim") {
  return {
    hash: `abcdef${index}`,
    author_name: actor,
    subject: `Change ${index + 1}`,
    date: "2026-08-20T00:00:00Z",
    files: [{ path: `guides/change-${index + 1}.md`, change: "modified" }],
  };
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/vault/platform-docs/activity"]}>
      <Routes>
        <Route path="/vault/:name/activity" element={<VaultActivityPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  getVaultActivityMock.mockReset();
});

afterEach(cleanup);

describe("Activity page redesign", () => {
  it("uses the compact Publish-style ledger and native activity date", async () => {
    getVaultActivityMock.mockResolvedValue({
      vault: "platform-docs",
      total: 2,
      activity: [activity(0), activity(1, "codex")],
    });
    renderPage();

    expect(await screen.findByText("Change 1")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 1, name: "Activity" }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("activity-ledger-header")).toHaveClass(
      "bg-surface-2/55",
      "border-border-strong",
    );
    const summary = screen.getByTestId("activity-summary");
    expect(within(summary).getByText("Git-backed")).toBeInTheDocument();
    expect(within(summary).getByText("People")).toBeInTheDocument();
    expect(within(summary).getByText("Agents")).toBeInTheDocument();
    expect(
      within(summary).getByText(/Every commit in this vault is recorded here/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("searchbox", { name: "Filter activity by author" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Filter by JY Kim (1 change)" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Quick")).toBeInTheDocument();
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
    expect(getVaultActivityMock).toHaveBeenCalledWith("platform-docs", {
      author: undefined,
      limit: 50,
    });

    const ledger = screen.getByRole("list", { name: "Vault activity" });
    expect(within(ledger).getByText("JY Kim")).toBeInTheDocument();
    expect(within(ledger).getByText("guides/change-1.md")).toBeInTheDocument();
    expect(within(ledger).getAllByText("Modified")).toHaveLength(2);
    expect(
      within(ledger).getByRole("link", { name: "Change 1" }),
    ).toHaveAttribute(
      "href",
      "/vault/platform-docs/doc/guides%2Fchange-1.md?commit=abcdef0",
    );
  });

  it("keeps quick author filtering usable for a longer history", async () => {
    const user = userEvent.setup();
    const entries = Array.from({ length: 9 }, (_, index) =>
      activity(index, index < 6 ? "JY Kim" : "codex"),
    );
    getVaultActivityMock.mockResolvedValueOnce({
      vault: "platform-docs",
      total: 9,
      activity: entries,
    });
    getVaultActivityMock.mockResolvedValueOnce({
      vault: "platform-docs",
      total: 6,
      activity: entries.slice(0, 6),
    });
    renderPage();

    expect(await screen.findByTestId("activity-controls")).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Filter by JY Kim (6 changes)" }),
    );

    await waitFor(() => {
      expect(getVaultActivityMock).toHaveBeenLastCalledWith("platform-docs", {
        author: "JY Kim",
        limit: 50,
      });
    });
    expect(await screen.findByText("Changes by JY Kim")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Filter by codex (3 changes)" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Filter by JY Kim (6 changes)" }),
    ).toHaveAttribute("aria-pressed", "true");
  });

  it("keeps empty and error states inside the same registry surface", async () => {
    getVaultActivityMock.mockResolvedValue({
      vault: "platform-docs",
      total: 0,
      activity: [],
    });
    const view = renderPage();
    expect(await screen.findByText("No activity yet")).toBeInTheDocument();

    view.unmount();
    getVaultActivityMock.mockRejectedValue(
      new Error("Activity service unavailable"),
    );
    renderPage();
    expect(await screen.findByText("Failed to load")).toBeInTheDocument();
    expect(
      screen.getByText("Activity service unavailable"),
    ).toBeInTheDocument();
  });
});
