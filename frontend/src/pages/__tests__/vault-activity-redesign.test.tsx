import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  act,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import VaultActivityPage from "@/pages/vault-activity";
import { CurrentUserProvider } from "@/contexts/current-user-context";

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
  it("hides the ledger during access proof and rejects old-revision rows and errors", async () => {
    let reject!: (error: Error) => void;
    getVaultActivityMock.mockResolvedValueOnce({ activity: [activity(0)] }).mockReturnValueOnce(new Promise((_r, j) => { reject = j; })).mockResolvedValue({ activity: [activity(1)] });
    const view = (checking: boolean, revision: number) => <MemoryRouter initialEntries={["/vault/platform-docs/activity"]}><CurrentUserProvider user={{ user_id: "a", username: "a", email: "a@x.test", display_name: null, is_admin: false, auth_method: "local", key_class: null }} checking={checking} revision={revision}><Routes><Route path="/vault/:name/activity" element={<VaultActivityPage />} /></Routes></CurrentUserProvider></MemoryRouter>;
    const { rerender } = render(view(false, 0));
    await screen.findByText("Change 1");
    rerender(view(true, 0));
    expect(screen.queryByText("Change 1")).not.toBeInTheDocument();
    rerender(view(false, 1));
    await waitFor(() => expect(getVaultActivityMock).toHaveBeenCalledTimes(2));
    rerender(view(true, 1));
    await act(async () => reject(new Error("Old permission error")));
    rerender(view(false, 2));
    await screen.findByText("Change 2");
    expect(screen.queryByText("Old permission error")).not.toBeInTheDocument();
  });
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
    expect(within(summary).getByText("Document history")).toBeInTheDocument();
    expect(within(summary).getByText("People")).toBeInTheDocument();
    expect(within(summary).getByText("Agents")).toBeInTheDocument();
    expect(
      within(summary).getByText(/Author filters search this recent window/i),
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

  it("keeps the current ledger visible while an author filter refreshes", async () => {
    const user = userEvent.setup();
    let resolveFiltered: ((value: { vault: string; total: number; activity: ReturnType<typeof activity>[] }) => void) | undefined;
    getVaultActivityMock
      .mockResolvedValueOnce({
        vault: "platform-docs",
        total: 2,
        activity: [activity(0), activity(1, "codex")],
      })
      .mockImplementationOnce(() => new Promise((resolve) => {
        resolveFiltered = resolve;
      }));
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Filter by JY Kim (1 change)" }));
    expect(await screen.findByText("Refreshing activity")).toBeInTheDocument();
    expect(screen.getByText("Change 1")).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Vault activity" }).closest("section")?.parentElement).toHaveAttribute("aria-busy", "true");

    resolveFiltered?.({ vault: "platform-docs", total: 1, activity: [activity(0)] });
    await waitFor(() => {
      expect(screen.queryByText("Refreshing activity")).not.toBeInTheDocument();
    });
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

  it("F16: author misses outside the recent 50 stay honest for Native and Git histories", async () => {
    getVaultActivityMock.mockResolvedValueOnce({ total: 50, activity: Array.from({ length: 50 }, (_, index) => activity(index)) }).mockResolvedValueOnce({ total: 0, activity: [] });
    const actor = userEvent.setup(); renderPage();
    await within(screen.getByTestId("activity-ledger-header")).findByText("50 loaded changes");
    expect(screen.queryByText("Git-backed")).not.toBeInTheDocument();
    expect(screen.getByText(/supports Git commits and Native revisions/)).toBeVisible();
    await actor.type(screen.getByRole("searchbox"), "Older author");
    expect(await screen.findByText(/No matches in the latest 50/)).toBeVisible();
    expect(screen.getByText(/Older matches may exist/)).toBeVisible();
  });
  it("ends busy after initial failure and offers recovery", async () => {
    getVaultActivityMock.mockRejectedValueOnce(new Error("Offline")).mockResolvedValueOnce({ activity: [activity(0)] });
    const actor = userEvent.setup(); const { container } = renderPage();
    await screen.findByText("Failed to load");
    expect(container.querySelector('[aria-busy="true"]')).toBeNull();
    await actor.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Change 1")).toBeVisible();
  });
  it("retains rows after a filter failure and retries the requested filter", async () => {
    getVaultActivityMock.mockResolvedValueOnce({ activity: [activity(0)] }).mockRejectedValueOnce(new Error("Offline")).mockResolvedValueOnce({ activity: [activity(1)] });
    const actor = userEvent.setup(); renderPage();
    await actor.click(await screen.findByRole("button", { name: "Filter by JY Kim (1 change)" }));
    await screen.findByText("Activity could not be refreshed");
    expect(screen.getByText("Change 1")).toBeVisible();
    await actor.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Change 2")).toBeVisible();
    expect(getVaultActivityMock).toHaveBeenLastCalledWith("platform-docs", { author: "JY Kim", limit: 50 });
  });

});
