import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router-dom";
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

function Destination() {
  const location = useLocation();
  return <output data-testid="destination">{JSON.stringify({ path: location.pathname, preview: !!location.state?.documentPreview })}</output>;
}

describe("Home recent updates", () => {
  it.each([true, false])("offers distinct preview and full Vault routes (preview=%s)", async preview => {
    render(<MemoryRouter><CurrentUserProvider user={user}><HomeRecentUpdates scope="all" /><Destination /></CurrentUserProvider></MemoryRouter>);
    await screen.findByText("All document");
    await userEvent.click(screen.getByRole("link", { name: preview ? "Preview All document" : "Open All document in work vault" }));
    expect(JSON.parse(screen.getByTestId("destination").textContent!)).toEqual({ path: "/vault/work/doc/All%20document", preview });
  });
  it("renders independent fixed-scope columns without tabs or duplicate preview IDs", async () => {
    recent.mockImplementation(async (_vault, _limit, options) => ({ scope: options?.scope ?? "all", changes: [row("Shared document")], next_cursor: options?.scope === "watching" ? "watch-next" : null }));
    render(<MemoryRouter><CurrentUserProvider user={user}><HomeRecentUpdates scope="all" /><HomeRecentUpdates scope="watching" /></CurrentUserProvider></MemoryRouter>);
    await waitFor(() => expect(screen.getAllByText("Shared document")).toHaveLength(2));
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
    const all = screen.getByRole("region", { name: "Recent updates" });
    const watching = screen.getByRole("region", { name: "Watched documents" });
    expect(within(all).getByRole("link", { name: "Preview Shared document" }).id).not.toBe(within(watching).getByRole("link", { name: "Preview Shared document" }).id);
    expect(within(all).queryByRole("button", { name: "Show more" })).not.toBeInTheDocument();
    await userEvent.click(within(watching).getByRole("button", { name: "Show more" }));
    expect(recent).toHaveBeenLastCalledWith(undefined, 6, { scope: "watching", cursor: "watch-next" });
  });
  it("requests Watching on the server and remembers it per account", async () => {
    renderFeed(); await screen.findByText("All document");
    recent.mockResolvedValue({ scope: "watching", next_cursor: null, changes: [row("Watched document")] });
    await userEvent.click(screen.getByRole("tab", { name: "Watched documents" }));
    expect(await screen.findByText("Watched document")).toBeInTheDocument();
    expect(recent).toHaveBeenLastCalledWith(undefined, 6, { scope: "watching", cursor: undefined });
    expect(screen.queryByText("All document")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Manage watches" })).toHaveAttribute("href", "/settings?tab=notifications");
    expect(localStorage.getItem("akb.homeRecentScope:first")).toBe("watching");
    expect(screen.queryByText("generated-id.md")).not.toBeInTheDocument();
    cleanup(); recent.mockResolvedValue({ scope: "all", changes: [] }); renderFeed("second");
    expect(screen.getByRole("tab", { name: "All documents" })).toHaveAttribute("aria-selected", "true");
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
    await userEvent.click(screen.getByRole("tab", { name: "Watched documents" })); await screen.findByText("Current watched");
    await act(async () => resolve({ scope: "all", changes: [row("Stale")] }));
    expect(screen.queryByText("Stale")).not.toBeInTheDocument();
    recent.mockResolvedValue({ scope: "watching", changes: [], next_cursor: null });
    act(() => window.dispatchEvent(new Event("akb:watch-changed")));
    await waitFor(() => expect(screen.queryByText("Current watched")).not.toBeInTheDocument());
  });
});
