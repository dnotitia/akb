import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { VaultCollectionsWorkspace } from "@/components/vault-collections-workspace";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { ResourceNavigationProvider, useResourceNavigationGuard } from "@/contexts/resource-navigation-context";
import { ApiError, browseVault } from "@/lib/api";

vi.mock("@/lib/api", async original => ({ ...await original<typeof import("@/lib/api")>(), browseVault: vi.fn() }));

const items = [
  { type: "collection", name: "guides", path: "guides" },
  { type: "collection", name: "architecture", path: "architecture" },
  { type: "collection", name: "indexing", path: "architecture/indexing" },
  { type: "collection", name: "overview", path: "overview" },
  { type: "document", name: "Runbook", path: "guides/runbook.md" },
  { type: "table", name: "services", path: "services", collection: "guides" },
  { type: "file", name: "diagram.svg", path: "file-id", uri: "akb://team/file/7ead8d7b-2861-4d64-8190-4cab55372b91", collection: "guides" },
];
function Location() {
  const location = useLocation();
  const navigate = useNavigate();
  return <><output aria-label="Current location">{location.pathname + location.search}</output>
    <button onClick={() => navigate("/vault/team?collection=guides")}>External collection shortcut</button>
    <button onClick={() => navigate(-1)}>Browser back</button><button onClick={() => navigate(1)}>Browser forward</button>
  </>;
}
function Guard({ blocked }: { blocked: boolean }) {
  useResourceNavigationGuard(blocked ? () => false : null);
  return null;
}
function mount(path = "/vault/team", blocked = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const content = (id: string, checking = false, revision = 0) => <QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><CurrentUserProvider user={{ user_id: id, username: id, email: `${id}@test.invalid`, display_name: null, is_admin: false, auth_method: "local", key_class: null }} checking={checking} revision={revision}><ResourceNavigationProvider><Guard blocked={blocked} /><VaultCollectionsWorkspace key={`${id}:${revision}`} vault="team" enabled={!checking} /><Location /></ResourceNavigationProvider></CurrentUserProvider></MemoryRouter></QueryClientProvider>;
  return { ...render(content("reader")), client, content };
}
function browseResponse(vault: string, collection?: string, depth = -1, options: Parameters<typeof browseVault>[3] = {}) {
  const prefix = collection ? collection + "/" : "";
  const query = options.name_query?.toLocaleLowerCase() ?? "";
  const selected = items.filter(item => {
    const path = item.type === "table" ? `${item.collection}/${item.path}` : item.type === "file" ? `${item.collection}/${item.name}` : item.path;
    return path.startsWith(prefix) && (depth < 0 || !path.slice(prefix.length).includes("/"))
      && (options.content_type !== "collections" || item.type === "collection")
      && (options.content_type !== "resources" || item.type !== "collection")
      && (!query || `${item.name} ${path}`.toLocaleLowerCase().includes(query));
  });
  return { vault, path: collection ?? "", complete: true, next_cursor: null, name_query: options.name_query,
    context: collection ? { type: "collection" as const, uri: `akb://${vault}/coll/${collection}`, path: collection, name: collection.split("/").at(-1)! } : undefined,
    items: selected };
}
beforeEach(() => { localStorage.clear(); vi.mocked(browseVault).mockReset().mockImplementation(async (...args) => browseResponse(...args)); });

describe("Vault collection workspace", () => {
  it("shows real collections and follows a native collection link to its document, table and file contents", async () => {
    const user = userEvent.setup();
    mount();
    const list = await screen.findByRole("list", { name: "Collections" });
    expect(within(list).getAllByRole("link")).toHaveLength(2);
    expect(within(list).queryByRole("link", { name: "Open collection overview" })).toBeNull();
    expect(within(list).getByRole("link", { name: "Open collection guides" })).toHaveTextContent("guides");
    await user.click(within(list).getByRole("link", { name: "Open collection guides" }));
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/vault/team?collection=guides");
    const contents = screen.getByRole("region", { name: "Collection resources" });
    expect(within(contents).getByRole("link", { name: /Runbook/ })).toHaveAttribute("href", "/vault/team/doc/guides%2Frunbook.md");
    expect(within(contents).getByRole("link", { name: /services/ })).toHaveAttribute("href", "/vault/team/table/services");
    expect(within(contents).getByRole("link", { name: /diagram.svg/ })).toHaveAttribute("href", "/vault/team/file/7ead8d7b-2861-4d64-8190-4cab55372b91");
    await user.click(screen.getByRole("link", { name: "All collections" }));
    expect(screen.getByRole("list", { name: "Collections" })).toBeVisible();
  });

  it("searches nested collections and restricts recent shortcuts to the current verified tree", async () => {
    localStorage.setItem("akb.recentCollections.v1:reader", JSON.stringify([
      { vault: "team", path: "guides", viewedAt: new Date(Date.now() - 1000).toISOString() },
      { vault: "team", path: "deleted", viewedAt: new Date(Date.now() - 2000).toISOString() },
      { vault: "other", path: "private", viewedAt: new Date(Date.now() - 3000).toISOString() },
    ]));
    const user = userEvent.setup();
    mount();
    const recent = await screen.findByRole("navigation", { name: "Recently visited collections" });
    expect(within(recent).getAllByRole("link")).toHaveLength(1);
    await user.type(screen.getByRole("textbox", { name: "Search collections and content" }), "index");
    expect(screen.queryByRole("navigation", { name: "Recently visited collections" })).toBeNull();
    expect(await screen.findByRole("link", { name: "Open collection architecture/indexing" })).toHaveAttribute("href", "/vault/team?collection=architecture%2Findexing");
  });

  it("resets a prior search when another navigator selects a collection", async () => {
    const user = userEvent.setup();
    mount();
    await screen.findByRole("link", { name: "Open collection guides" });
    await user.type(screen.getByRole("textbox", { name: "Search collections and content" }), "index");
    await user.click(screen.getByRole("button", { name: "External collection shortcut" }));
    expect(screen.getByRole("textbox", { name: "Search collections and content" })).toHaveValue("");
    expect(screen.getByRole("region", { name: "Collection resources" })).toHaveTextContent("Runbook");
    expect(screen.queryByRole("list", { name: "Matching collections" })).toBeNull();
  });

  it("returns one level up after a direct nested entry and preserves back/forward navigation", async () => {
    const user = userEvent.setup();
    mount("/vault/team?collection=architecture%2Findexing");
    const trail = await screen.findByRole("navigation", { name: "Collection location" });
    const parent = await within(trail).findByRole("link", { name: "Back to collection architecture" });
    expect(parent).toHaveAttribute("href", "/vault/team?collection=architecture");
    expect(within(trail).getByRole("heading", { name: "indexing" }).closest("li")).toHaveAttribute("aria-current", "page");
    expect(within(trail).getByRole("link", { name: "All collections" })).toHaveAttribute("href", "/vault/team");
    await user.click(parent);
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/vault/team?collection=architecture");
    expect(screen.getByRole("link", { name: "Open collection architecture/indexing" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Browser back" }));
    expect(screen.getByRole("heading", { name: "indexing" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Browser forward" }));
    expect(screen.getByRole("heading", { name: "architecture" })).toBeVisible();
    await user.click(screen.getByRole("link", { name: "All collections" }));
    expect(screen.getByLabelText("Current location")).toHaveTextContent(/^\/vault\/team$/);
  });

  it("returns to the selected result's parent after searching across branches", async () => {
    const user = userEvent.setup();
    mount();
    await screen.findByRole("heading", { name: "Collections" });
    await user.type(screen.getByRole("textbox", { name: "Search collections and content" }), "indexing");
    await user.click(await screen.findByRole("link", { name: "Open collection architecture/indexing" }));
    expect(screen.getByRole("textbox", { name: "Search collections and content" })).toHaveValue("");
    await user.click(screen.getByRole("link", { name: "Back to collection architecture" }));
    expect(screen.getByRole("heading", { name: "architecture" })).toBeVisible();
  });

  it("keeps encoded native parent links and bypasses draft guards only for modified clicks", async () => {
    const path = "팀 자료 & 운영/설계 #1/child?";
    vi.mocked(browseVault).mockResolvedValue({ vault: "team", path: "", complete: true, next_cursor: null, items: [
      { type: "collection", name: "팀 자료 & 운영", path: "팀 자료 & 운영" },
      { type: "collection", name: "설계 #1", path: "팀 자료 & 운영/설계 #1" },
      { type: "collection", name: "child?", path },
    ] });
    mount(`/vault/team?collection=${encodeURIComponent(path)}`, true);
    const parent = await screen.findByRole("link", { name: "Back to collection 팀 자료 & 운영/설계 #1" });
    expect(parent).toHaveAttribute("href", `/vault/team?collection=${encodeURIComponent("팀 자료 & 운영/설계 #1")}`);
    expect(fireEvent.click(parent, { ctrlKey: true })).toBe(true);
    expect(fireEvent.click(parent, { metaKey: true })).toBe(true);
    expect(fireEvent.click(parent)).toBe(false);
    expect(screen.getByLabelText("Current location")).toHaveTextContent(`/vault/team?collection=${encodeURIComponent(path)}`);
    expect(screen.getByRole("heading", { name: "child?" })).toBeVisible();
  });

  it("offers only the root return for an unknown collection", async () => {
    mount("/vault/team?collection=private%2Fmissing");
    await screen.findByText(/This collection is unavailable/);
    const trail = screen.getByRole("navigation", { name: "Collection location" });
    expect(within(trail).getAllByRole("link")).toHaveLength(1);
    expect(within(trail).getByRole("link", { name: "All collections" })).toHaveAttribute("href", "/vault/team");
    expect(within(trail).queryByText("private")).toBeNull();
  });

  it("does not expose a fabricated missing context through search", async () => {
    vi.mocked(browseVault).mockImplementation(async (_vault, collection) => ({ vault: "team", path: collection || "", complete: true, next_cursor: null,
      context: { type: "collection", uri: "akb://team/coll/deleted", path: "deleted", name: "deleted" }, items: [],
    }));
    mount("/vault/team?collection=deleted");
    await screen.findByText(/This collection is unavailable/);
    await userEvent.type(screen.getByRole("textbox", { name: "Search collections and content" }), "deleted");
    expect(screen.getByText(/This collection is unavailable/)).toBeVisible();
    expect(screen.queryByRole("link", { name: "Open collection deleted" })).toBeNull();
  });

  it("omits recursive inventory and offers the keyboard paging fallback", async () => {
    vi.mocked(browseVault).mockResolvedValueOnce({ vault: "team", path: "", complete: false, next_cursor: "next", items });
    const user = userEvent.setup();
    mount();
    const card = await screen.findByRole("link", { name: "Open collection guides" });
    expect(card).not.toHaveTextContent("1 document");
    expect(vi.mocked(browseVault).mock.calls.filter(call => call[3]?.content_type === "collections")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Load more collections" }));
    await waitFor(() => expect(vi.mocked(browseVault)).toHaveBeenLastCalledWith("team", undefined, 0, expect.objectContaining({ page_size: 100, cursor: "next", content_type: "collections" })));
  });

  it("does not inherit another account's cached tree while access is being verified", async () => {
    const view = mount();
    await screen.findByRole("link", { name: "Open collection guides" });
    let resolve!: (value: Awaited<ReturnType<typeof browseVault>>) => void;
    vi.mocked(browseVault).mockReturnValue(new Promise(done => { resolve = done; }));
    view.rerender(view.content("replacement", true));
    expect(screen.queryByRole("link", { name: "Open collection guides" })).toBeNull();
    view.rerender(view.content("replacement", false, 1));
    expect(screen.queryByRole("link", { name: "Open collection guides" })).toBeNull();
    await act(async () => resolve({ vault: "team", path: "", complete: true, next_cursor: null, items: [{ type: "collection", name: "New scope", path: "new" }] }));
    expect(await screen.findByRole("link", { name: "Open collection new" })).toBeVisible();
    expect(screen.queryByRole("link", { name: "Open collection guides" })).toBeNull();
  });

  it("does not clear a newer successful snapshot when a cancelled request later denies access", async () => {
    const view = mount();
    await screen.findByRole("link", { name: "Open collection guides" });
    let rejectOld!: (error: Error) => void;
    vi.mocked(browseVault).mockReturnValueOnce(new Promise((_resolve, reject) => { rejectOld = reject; }));
    let previous!: Promise<void>;
    await act(async () => { previous = view.client.refetchQueries({ queryKey: ["vault-tree"] }); });
    vi.mocked(browseVault).mockResolvedValue({ vault: "team", path: "", complete: true, next_cursor: null, items: [{ type: "collection", name: "Current", path: "current" }] });
    await act(async () => { await view.client.refetchQueries({ queryKey: ["vault-tree"] }); });
    expect(await screen.findByRole("link", { name: "Open collection current" })).toBeVisible();
    await act(async () => { rejectOld(new ApiError("Forbidden", 403, "Forbidden")); await previous; });
    expect(screen.getByRole("link", { name: "Open collection current" })).toBeVisible();
  });

  it("hides a denied cached directory and exposes recovery", async () => {
    const view = mount();
    await screen.findByRole("link", { name: "Open collection guides" });
    vi.mocked(browseVault).mockRejectedValue(new ApiError("Forbidden", 403, "Forbidden"));
    await act(async () => { await view.client.invalidateQueries({ queryKey: ["vault-tree"] }); });
    await waitFor(() => expect(screen.queryByRole("link", { name: "Open collection guides" })).toBeNull());
    expect(screen.getByRole("button", { name: "Retry collections" })).toBeVisible();
    expect(view.client.getQueryData(["vault-tree", "reader", 0, "team", "unarchived"])).toBeUndefined();
    vi.mocked(browseVault).mockRejectedValue(new ApiError("Unavailable", 503, "Unavailable"));
    await userEvent.click(screen.getByRole("button", { name: "Retry collections" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Retry collections" })).not.toHaveAttribute("aria-busy", "true"));
    expect(screen.queryByRole("link", { name: "Open collection guides" })).toBeNull();
    // Replacing the observer cannot restore the revoked cached snapshot.
    view.rerender(<></>);
    view.rerender(view.content("reader"));
    await screen.findByRole("button", { name: "Retry collections" });
    expect(screen.queryByRole("link", { name: "Open collection guides" })).toBeNull();
    vi.mocked(browseVault).mockResolvedValue({ vault: "team", path: "", complete: true, next_cursor: null, items });
    await userEvent.click(screen.getByRole("button", { name: "Retry collections" }));
    expect(await screen.findByRole("link", { name: "Open collection guides" })).toBeVisible();
  });
});
