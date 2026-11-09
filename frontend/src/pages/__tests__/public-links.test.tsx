import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Link, MemoryRouter, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import PublicLinksPage from "@/pages/public-links";
import { ApiError, deletePublication, listPublicationDirectory, listVaults, type DirectoryPublication } from "@/lib/api";

vi.mock("@/lib/api", async original => ({ ...await original<typeof import("@/lib/api")>(), listPublicationDirectory: vi.fn(), listVaults: vi.fn(), deletePublication: vi.fn() }));

const item: DirectoryPublication = {
  slug: "guide", title: "Operating guide", vault: "team vault", resource_type: "document", resource_uri: "akb://team vault/doc/guide.md", resource_path: "guide.md",
  share_url: "https://example.test/p/guide", mode: "live", view_count: 2, max_views: null, expires_at: null, allow_embed: true,
  section_filter: null, snapshot_at: null, created_at: "2026-10-01T00:00:00Z", password_protected: false,
  availability: "active", can_unpublish: true, unpublish_disabled_reason: null,
};
const result = (items = [item]) => ({ publications: items, complete: true, next_cursor: null, total: items.length, observed_at: "2026-10-28T00:00:00Z" });
function Location() { const location = useLocation(); return <output data-testid="location">{location.search}</output>; }
function element(client: QueryClient, account = "a", revision = 0, checking = false) {
  return <QueryClientProvider client={client}><CurrentUserProvider user={{ user_id: account } as never} revision={revision} checking={checking}>
    <MemoryRouter initialEntries={["/publications"]}><Link to="/publications?vault=team%20vault&status=all&expiry=soon">Scoped links</Link><Location /><PublicLinksPage /></MemoryRouter>
  </CurrentUserProvider></QueryClientProvider>;
}
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listVaults).mockResolvedValue({ vaults: [{ id: "v", name: "team vault" }] });
  vi.mocked(listPublicationDirectory).mockResolvedValue(result());
  vi.mocked(deletePublication).mockResolvedValue({ deleted: 1 } as never);
});
afterEach(cleanup);

it("loads URL scopes and server filters while preserving navigation state", async () => {
  const user = userEvent.setup();
  render(element(new QueryClient({ defaultOptions: { queries: { retry: false } } })));
  await screen.findByRole("link", { name: item.title! });
  expect(listPublicationDirectory).toHaveBeenLastCalledWith(expect.objectContaining({ vault: undefined, status: "active", expiry: "any", page_size: 30, signal: expect.any(AbortSignal) }));
  await user.click(screen.getByRole("link", { name: "Scoped links" }));
  await waitFor(() => expect(listPublicationDirectory).toHaveBeenLastCalledWith(expect.objectContaining({ vault: "team vault", status: "all", expiry: "soon" })));
  await user.type(screen.getByRole("textbox", { name: "Search publications" }), "whole corpus");
  await waitFor(() => expect(listPublicationDirectory).toHaveBeenLastCalledWith(expect.objectContaining({ vault: "team vault", q: "whole corpus", expiry: "soon" })));
  expect(Object.fromEntries(new URLSearchParams(screen.getByTestId("location").textContent!))).toEqual({ vault: "team vault", status: "all", expiry: "soon", q: "whole corpus" });
  expect(screen.getByRole("link", { name: "Open in vault" })).toHaveAttribute("href", "/vault/team%20vault/publications");
});

it("keeps reader links visible and explains disabled unpublish", async () => {
  vi.mocked(listPublicationDirectory).mockResolvedValue(result([{ ...item, can_unpublish: false, unpublish_disabled_reason: "Writer access required" }]));
  const user = userEvent.setup();
  render(element(new QueryClient()));
  await user.click(await screen.findByRole("button", { name: `More actions for ${item.title}` }));
  const action = screen.getByRole("menuitem", { name: /Unpublish/ });
  expect(action).toHaveAttribute("aria-disabled", "true");
  expect(action).toHaveAccessibleDescription("Writer access required");
  expect(deletePublication).not.toHaveBeenCalled();
});

it("unpublishes using the row's Vault and returns focus after refreshing", async () => {
  const user = userEvent.setup();
  render(element(new QueryClient()));
  await user.click(await screen.findByRole("button", { name: `More actions for ${item.title}` }));
  await user.click(screen.getByRole("menuitem", { name: /Unpublish/ }));
  vi.mocked(listPublicationDirectory).mockResolvedValue(result([]));
  const dialog = screen.getByRole("dialog");
  await user.click(within(dialog).getByRole("button", { name: "Unpublish" }));
  await waitFor(() => expect(deletePublication).toHaveBeenCalledWith("team vault", "guide"));
  expect(await screen.findByText("Nothing published right now")).toBeInTheDocument();
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Search publications" })).toHaveFocus());
});

it("removes stale links on denied refresh and on account verification", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(element(client));
  await screen.findByRole("link", { name: item.title! });
  vi.mocked(listPublicationDirectory).mockRejectedValue(new ApiError("Access changed", 403, null));
  await userEvent.click(screen.getByRole("button", { name: "Refresh publications" }));
  await waitFor(() => expect(screen.queryByRole("link", { name: item.title! })).not.toBeInTheDocument());
  view.rerender(element(client, "b", 1, true));
  expect(screen.queryByRole("link", { name: item.title! })).not.toBeInTheDocument();
});

it.each([400, 403])("recovers from a paging boundary error (%s)", async status => {
  vi.mocked(listPublicationDirectory).mockResolvedValueOnce({ ...result(), complete: false, next_cursor: "next" });
  vi.mocked(listPublicationDirectory).mockRejectedValue(new ApiError("Refresh this list", status, null));
  render(element(new QueryClient({ defaultOptions: { queries: { retry: false } } })));
  await screen.findByRole("link", { name: item.title! });
  fireEvent.click(screen.getByRole("button", { name: "Load more publications" }));
  expect(await screen.findByText("Refresh this list", { exact: false })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Retry: load more publications" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: item.title! }) !== null).toBe(status !== 403);
  expect(screen.getByRole("button", { name: status === 400 ? "Refresh list" : "Retry publications" })).toBeInTheDocument();
});
