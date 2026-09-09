import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { HomeRecentUpdates } from "@/components/home-recent-updates";
import { getRecent, ApiError } from "@/lib/api";
import { CurrentUserProvider } from "@/contexts/current-user-context";

vi.mock("@/lib/api", async () => ({ ...await vi.importActual("@/lib/api"), getRecent: vi.fn() }));
const recent = vi.mocked(getRecent);
const user = { user_id: "first", username: "one", email: "one@example.com", display_name: "One", is_admin: false, auth_method: "local", key_class: null };
const row = (id: string) => ({ resource_id: id, doc_id: id, vault: "work", path: "guides/generated-id.md", title: id, changed_at: "2026-09-01T00:00:00Z" });
function renderFeed(userId = "first") {
  return render(<MemoryRouter><CurrentUserProvider user={{ ...user, user_id: userId }}><HomeRecentUpdates /></CurrentUserProvider></MemoryRouter>);
}
beforeEach(() => { recent.mockReset(); localStorage.clear(); recent.mockResolvedValue({ scope: "all", next_cursor: null, changes: [row("All document")] }); });
afterEach(cleanup);

describe("Home recent updates", () => {
  it("requests Watching on the server and remembers it per account", async () => {
    renderFeed(); await screen.findByText("All document");
    recent.mockResolvedValue({ scope: "watching", next_cursor: null, changes: [row("Watched document")] });
    await userEvent.click(screen.getByRole("tab", { name: "Watching" }));
    expect(await screen.findByText("Watched document")).toBeInTheDocument();
    expect(recent).toHaveBeenLastCalledWith(undefined, 6, { scope: "watching", cursor: undefined });
    expect(screen.queryByText("All document")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Manage watches" })).toHaveAttribute("href", "/settings?tab=notifications");
    expect(localStorage.getItem("akb.homeRecentScope:first")).toBe("watching");
    expect(screen.queryByText("generated-id.md")).not.toBeInTheDocument();
    cleanup(); recent.mockResolvedValue({ scope: "all", changes: [] }); renderFeed("second");
    expect(screen.getByRole("tab", { name: "All" })).toHaveAttribute("aria-selected", "true");
  });

  it("does not mistake a legacy unfiltered response for Watching", async () => {
    localStorage.setItem("akb.homeRecentScope:first", "watching");
    recent.mockResolvedValue({ changes: [row("Not watched")] }); renderFeed();
    expect(await screen.findByText(/Watching is not supported/)).toBeInTheDocument();
    expect(screen.queryByText("Not watched")).not.toBeInTheDocument();
  });

  it("distinguishes empty, disabled, and failed Watching", async () => {
    localStorage.setItem("akb.homeRecentScope:first", "watching");
    recent.mockResolvedValue({ scope: "watching", changes: [] }); renderFeed();
    expect(await screen.findByText("No watched documents yet")).toBeInTheDocument();
    cleanup(); recent.mockRejectedValue(new ApiError("Disabled", 503, { code: "notifications_disabled" })); renderFeed();
    expect(await screen.findByText(/Watching is not supported/)).toBeInTheDocument();
    cleanup(); recent.mockRejectedValue(new Error("Offline")); renderFeed();
    expect(await screen.findByText("Could not load recent updates.")).toBeInTheDocument();
    expect(screen.queryByText("No watched documents yet")).not.toBeInTheDocument();
  });

  it("retains rows on pagination failure and retries the same cursor without duplicates", async () => {
    recent.mockResolvedValue({ scope: "all", changes: [row("First")], next_cursor: "next" }); renderFeed();
    await screen.findByText("First"); recent.mockRejectedValueOnce(new Error("Offline"));
    await userEvent.click(screen.getByRole("button", { name: "Show more" }));
    expect(await screen.findByText(/Your current list is still available/)).toBeInTheDocument();
    expect(screen.getByText("First")).toBeInTheDocument();
    recent.mockResolvedValue({ scope: "all", changes: [row("First"), row("Second")], next_cursor: null });
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText("Second"); expect(screen.getAllByText("First")).toHaveLength(1);
    expect(recent).toHaveBeenLastCalledWith(undefined, 6, { scope: "all", cursor: "next" });
  });

  it("ignores a stale request after changing tabs and refreshes on Watch changes", async () => {
    let resolve!: (value: Awaited<ReturnType<typeof getRecent>>) => void;
    recent.mockImplementationOnce(() => new Promise(done => { resolve = done; })); renderFeed();
    recent.mockResolvedValue({ scope: "watching", changes: [row("Current watched")], next_cursor: null });
    await userEvent.click(screen.getByRole("tab", { name: "Watching" })); await screen.findByText("Current watched");
    await act(async () => resolve({ scope: "all", changes: [row("Stale")] }));
    expect(screen.queryByText("Stale")).not.toBeInTheDocument();
    recent.mockResolvedValue({ scope: "watching", changes: [], next_cursor: null });
    act(() => window.dispatchEvent(new Event("akb:watch-changed")));
    await waitFor(() => expect(screen.queryByText("Current watched")).not.toBeInTheDocument());
  });
});
