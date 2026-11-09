import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation, useParams } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { ReadingWorkspaceProvider } from "@/contexts/reading-workspace-context";
import { ResourceNavigationProvider, useResourceNavigationGuard } from "@/contexts/resource-navigation-context";
import { DocumentReadingWorkspace } from "@/components/document-reading-workspace";
import { ApiError, getDocument, getRelations, type CurrentUser } from "@/lib/api";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import { emptyReadingSession, writeReadingSession, READING_SESSION_PREFIX } from "@/lib/reading-session";
import { useReadingChrome } from "@/contexts/reading-chrome-context";

vi.mock("@/lib/api", async original => ({ ...await original<typeof import("@/lib/api")>(), getDocument: vi.fn(), getRelations: vi.fn() }));
vi.mock("@/components/markdown-render", () => ({ MarkdownRender: ({ markdown, headingIdPrefix, leadingMetadata, documentTitle }: { markdown: string; headingIdPrefix: string; leadingMetadata?: ReactNode; documentTitle?: string }) => <div>
  {documentTitle && <h2>{documentTitle}</h2>}{leadingMetadata}
  <h3 id={`${headingIdPrefix}section`}>Reference section</h3><p>{markdown}</p><a href="/vault/alpha/doc/c.md">Follow another reference</a>
</div> }));
const docs: Record<string, { path: string; title: string; content: string; current_commit: string }> = {
  "a.md": { path: "a.md", title: "Document A", content: "Body A", current_commit: "aa" },
  "b.md": { path: "b.md", title: "Document B", content: "Body B", current_commit: "bb" },
  "c.md": { path: "c.md", title: "Document C", content: "Body C", current_commit: "cc" },
};
function Primary({ blocked }: { blocked: boolean }) {
  const chrome = useReadingChrome();
  const { id } = useParams();
  const location = useLocation();
  useResourceNavigationGuard(blocked ? () => false : null);
  return <>{chrome?.toolbarHost && createPortal(chrome.referenceControl, chrome.toolbarHost)}<output data-testid="location">{location.pathname}</output><div id="document-reading-canvas" tabIndex={-1}>
    Primary {id}<a href="/vault/alpha/doc/b.md">Reference B</a><a href="/vault/alpha/doc/c.md">Reference C</a>
    <a href="https://example.org/doc">External source</a><a>Inert model name</a>
    <a href="/vault/alpha/doc/b.md" target="_blank" data-markdown-reference-runtime-url="/vault/alpha/doc/b.md">Resolved reference</a>
    <a href="/vault/alpha/doc/b.md" download>Download reference</a>
    <a href="/vault/alpha/doc/b.md?commit=older">Historical reference</a>
  </div></>;
}
function setup({ blocked = false, revision = 0, client = new QueryClient({ defaultOptions: { queries: { retry: false } } }) } = {}) {
  const renderTree = (nextRevision: number) => <QueryClientProvider client={client}><MemoryRouter initialEntries={["/vault/alpha/doc/a.md"]}>
    <CurrentUserProvider user={{ user_id: "reader" } as CurrentUser} revision={nextRevision}>
      <ResourceNavigationProvider><ReadingWorkspaceProvider><Routes>
        <Route path="/vault/:name/doc/:id" element={<DocumentReadingWorkspace enabled><Primary blocked={blocked} /></DocumentReadingWorkspace>} />
        <Route path="/vault/:name" element={<p>Vault overview</p>} />
      </Routes></ReadingWorkspaceProvider></ResourceNavigationProvider>
    </CurrentUserProvider></MemoryRouter></QueryClientProvider>;
  const result = render(renderTree(revision));
  return { ...result, rerenderRevision: (value: number) => result.rerender(renderTree(value)) };
}
beforeEach(() => {
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ width: 1200, height: 800, top: 0, left: 0, right: 1200, bottom: 800, x: 0, y: 0, toJSON: () => ({}) });
  vi.mocked(getDocument).mockImplementation(async (_vault, path) => docs[path]);
  vi.mocked(getRelations).mockResolvedValue({ uri: "akb://alpha/doc/a.md", relations: [{ direction: "outgoing", relation: "links_to", uri: "akb://alpha/doc/b.md", name: "Document B", kind: "implicit" }] });
});
afterEach(() => { vi.restoreAllMocks(); vi.clearAllMocks(); });

describe("Reading Studio", () => {
  it("preserves modified-click and download destinations", async () => {
    setup(); await screen.findByRole("tab", { name: "Document A" });
    for (const name of ["Reference B", "Download reference"]) {
      const event = new MouseEvent("click", { bubbles: true, cancelable: true, ctrlKey: name === "Reference B" });
      screen.getByRole("link", { name }).dispatchEvent(event);
      expect(event.defaultPrevented).toBe(false);
    }
    expect(screen.queryByRole("complementary", { name: "Document B" })).not.toBeInTheDocument();
  });

  it("opens a resolved resource in preview despite the renderer's default blank target", async () => {
    const user = userEvent.setup(); setup(); await screen.findByRole("tab", { name: "Document A" });
    await user.click(screen.getByRole("link", { name: "Resolved reference" }));
    expect(await screen.findByRole("complementary", { name: "Document B" })).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/vault/alpha/doc/a.md");
  });

  it("does not label current metadata time as a historical reference's edit time", async () => {
    vi.mocked(getDocument).mockImplementation(async (_vault, path) => ({ ...docs[path], updated_at: "2026-10-27T03:00:00Z" }));
    const user = userEvent.setup(); setup(); await screen.findByRole("tab", { name: "Document A" });
    await user.click(screen.getByRole("link", { name: "Historical reference" }));
    const pane = await screen.findByRole("complementary", { name: "Document B" });
    expect(within(pane).getByText("Version older")).toBeInTheDocument();
    expect(pane.querySelector("time")).toBeNull();
  });

  it("keeps references keyboard accessible without a visible label or graph navigation", async () => {
    const user = userEvent.setup(); setup();
    const trigger = await screen.findByRole("button", { name: /^Show document references/ });
    await waitFor(() => expect(trigger).toHaveAccessibleName("Show document references (1)"));
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("complementary", { name: "Document references" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Explore all connections" })).not.toBeInTheDocument();
    trigger.focus(); await user.keyboard("{Enter}");
    expect(await screen.findByRole("complementary", { name: "Document references" })).toBeInTheDocument();
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    await user.click(screen.getByRole("button", { name: "Close references" }));
    expect(trigger).toHaveFocus();
  });

  it("shows a confirmed zero reference count without opening the panel", async () => {
    vi.mocked(getRelations).mockResolvedValue({ uri: "akb://alpha/doc/a.md", relations: [] });
    setup();
    const trigger = await screen.findByRole("button", { name: /^Show document references/ });
    await waitFor(() => expect(trigger).toHaveAccessibleName("Show document references (0)"));
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("does not present a failed reference request as a zero count", async () => {
    vi.mocked(getRelations).mockRejectedValue(new Error("Unavailable"));
    const user = userEvent.setup(); setup();
    const trigger = await screen.findByRole("button", { name: /^Show document references/ });
    await user.click(trigger);
    await screen.findByText("References could not be loaded.");
    expect(trigger.querySelector(".reading-reference-count")).toBeNull();
    expect(trigger).toHaveAccessibleName("Show document references");
  });

  it("previews without replacing or scrolling the main document, and only keeps explicit tabs", async () => {
    const user = userEvent.setup(); setup();
    await screen.findByRole("tab", { name: "Document A" });
    const canvas = document.getElementById("document-reading-canvas")!;
    canvas.scrollTop = 240; fireEvent.scroll(canvas);
    await user.click(screen.getByRole("link", { name: "Reference B" }));
    const pane = await screen.findByRole("complementary", { name: "Document B" });
    expect(within(pane).getByText("Body B")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/vault/alpha/doc/a.md");
    expect(document.getElementById("document-reading-canvas")).toBe(canvas);
    expect(canvas.scrollTop).toBe(240);
    expect(screen.getAllByRole("tab")).toHaveLength(1);
    await user.click(within(pane).getByRole("button", { name: "Keep in tabs" }));
    expect(screen.getAllByRole("tab")).toHaveLength(2);
    expect(screen.getByRole("tab", { name: "Document A" })).toHaveAttribute("aria-selected", "true");
    await user.click(screen.getByRole("button", { name: "Close reference preview" }));
    await waitFor(() => expect(screen.getByRole("link", { name: "Reference B" })).toHaveFocus());
    await user.click(screen.getByRole("tab", { name: "Document B" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/vault/alpha/doc/b.md"));
    await user.click(screen.getByRole("tab", { name: "Document A" }));
    await waitFor(() => expect(canvas.scrollTop).toBe(240));
  });

  it("keeps preview Back independent from browser navigation, and deduplicates kept documents", async () => {
    const user = userEvent.setup(); setup(); await screen.findByRole("tab", { name: "Document A" });
    await user.click(screen.getByRole("link", { name: "Reference B" }));
    const pane = await screen.findByRole("complementary", { name: "Document B" });
    await user.click(within(pane).getByRole("button", { name: "Keep in tabs" }));
    await user.click(within(pane).getByRole("link", { name: "Follow another reference" }));
    await screen.findByRole("complementary", { name: "Document C" });
    await user.click(screen.getByRole("button", { name: "Previous reference" }));
    const previous = await screen.findByRole("complementary", { name: "Document B" });
    await user.click(within(previous).getByRole("button", { name: "Kept in tabs" }));
    expect(screen.getAllByRole("tab")).toHaveLength(2);
    expect(screen.getByTestId("location")).toHaveTextContent("/vault/alpha/doc/a.md");
  });

  it("does not let an older response replace a newer preview", async () => {
    let resolveB!: (value: typeof docs[string]) => void;
    vi.mocked(getDocument).mockImplementation((_vault, path) => path === "b.md" ? new Promise(resolve => { resolveB = resolve; }) : Promise.resolve(docs[path]));
    const user = userEvent.setup(); setup(); await screen.findByRole("tab", { name: "Document A" });
    await user.click(screen.getByRole("link", { name: "Reference B" }));
    await user.click(screen.getByRole("link", { name: "Reference C" }));
    await screen.findByRole("complementary", { name: "Document C" });
    await act(async () => { resolveB(docs["b.md"]); });
    expect(screen.getByText("Body C")).toBeInTheDocument();
    expect(screen.queryByText("Body B")).toBeNull();
  });

  it("consults the editor guard before removing an active tab", async () => {
    const user = userEvent.setup(); setup({ blocked: true }); await screen.findByRole("tab", { name: "Document A" });
    await user.click(screen.getByRole("button", { name: "Close Document A" }));
    expect(screen.getByRole("tab", { name: "Document A" })).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/vault/alpha/doc/a.md");
  });

  it("supports manual keyboard tab activation, closing inactive tabs, and reopening", async () => {
    const user = userEvent.setup(); setup(); await screen.findByRole("tab", { name: "Document A" });
    await user.click(screen.getByRole("link", { name: "Reference B" }));
    await screen.findByRole("complementary", { name: "Document B" });
    await user.click(screen.getByRole("button", { name: "Keep in tabs" }));
    await user.click(screen.getByRole("button", { name: "Close reference preview" }));
    await waitFor(() => expect(screen.getByRole("link", { name: "Reference B" })).toHaveFocus());
    screen.getByRole("tab", { name: "Document A" }).focus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Document B" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: "Document A" })).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{Delete}");
    expect(screen.queryByRole("tab", { name: "Document B" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Open document list" }));
    await user.click(screen.getByRole("tab", { name: /^Recently closed/ }));
    await user.click(screen.getByRole("button", { name: /Document B.*b.md/ }));
    await screen.findByRole("tab", { name: "Document B" });
  });

  it("clears private inactive tab metadata after a new access proof", async () => {
    const user = userEvent.setup(); const { rerenderRevision } = setup(); await screen.findByRole("tab", { name: "Document A" });
    await user.click(screen.getByRole("link", { name: "Reference B" }));
    await screen.findByRole("complementary", { name: "Document B" });
    await user.click(screen.getByRole("button", { name: "Keep in tabs" }));
    vi.mocked(getDocument).mockImplementation(async (_vault, path) => {
      if (path === "b.md") throw new ApiError("Forbidden", 403, null);
      return docs[path];
    });
    rerenderRevision(1);
    await waitFor(() => expect(screen.queryByRole("tab", { name: "Document B" })).toBeNull());
    expect(screen.queryByText("Body B")).toBeNull();
  });

  it("closes the active tab after navigation, focuses its neighbor, then returns the last tab to the Vault", async () => {
    const user = userEvent.setup(); setup(); await screen.findByRole("tab", { name: "Document A" });
    await user.click(screen.getByRole("link", { name: "Reference B" }));
    await screen.findByRole("complementary", { name: "Document B" });
    await user.click(screen.getByRole("button", { name: "Keep in tabs" }));
    await user.click(screen.getByRole("button", { name: "Close reference preview" }));
    await user.click(screen.getByRole("button", { name: "Close Document A" }));
    await waitFor(() => expect(screen.getByRole("tab", { name: "Document B" })).toHaveFocus());
    expect(screen.getByRole("tab", { name: "Document B" })).toHaveAttribute("tabindex", "0");
    expect(screen.queryByRole("tab", { name: "Document A" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Close Document B" }));
    await screen.findByText("Vault overview");
  });

  it("uses a focus-trapped reference sheet when the actual reader width is small", async () => {
    vi.mocked(HTMLElement.prototype.getBoundingClientRect).mockReturnValue({ width: 650 } as DOMRect);
    const user = userEvent.setup(); setup(); await screen.findByRole("tab", { name: "Document A" });
    await user.click(screen.getByRole("link", { name: "Reference B" }));
    await screen.findByRole("dialog", { name: "Reference preview" });
    await screen.findByRole("heading", { name: "Document B" });
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(screen.getByRole("link", { name: "Reference B" })).toHaveFocus());
  });
  it("reorders through the document list and closes with the middle button", async () => {
    const user = userEvent.setup(); setup(); await screen.findByRole("tab", { name: "Document A" });
    await user.click(screen.getByRole("link", { name: "Reference B" }));
    await user.click(await screen.findByRole("button", { name: "Keep in tabs" }));
    await user.click(screen.getByRole("button", { name: "Close reference preview" }));
    await waitFor(() => expect(screen.getByRole("link", { name: "Reference B" })).toHaveFocus());
    await user.click(screen.getByRole("button", { name: "Open document list" }));
    await user.click(screen.getByRole("button", { name: "Tab actions for Document B" }));
    await user.click(screen.getByRole("menuitem", { name: "Move Document B left" }));
    expect(within(screen.getByRole("tablist", { name: "Open documents" })).getAllByRole("tab").map(tab => tab.textContent)).toEqual(["Document B", "Document A"]);
    await user.keyboard("{Escape}");
    fireEvent(screen.getByRole("tab", { name: "Document B" }), new MouseEvent("auxclick", { bubbles: true, button: 1 }));
    expect(screen.queryByRole("tab", { name: "Document B" })).toBeNull();
    expect(screen.getByRole("tab", { name: "Document A" })).toHaveAttribute("aria-selected", "true");
  });

  it("restores saved tabs and the side document only after their access proof succeeds", async () => {
    const session = emptyReadingSession();
    const saved = { vault: "alpha", path: "b.md", title: "Stale secret title", href: "/vault/alpha/doc/b.md" };
    session.tabs = [saved]; session.companion = saved; writeReadingSession("reader", session);
    let resolve!: (value: typeof docs["b.md"]) => void;
    const pending = new Promise<typeof docs["b.md"]>(done => { resolve = done; });
    vi.mocked(getDocument).mockImplementation(async (_vault, path) => path === "b.md" ? pending : docs[path]);
    setup();
    expect(screen.queryByText("Stale secret title")).toBeNull();
    expect(screen.queryByRole("tab", { name: "Document B" })).toBeNull();
    await act(async () => resolve(docs["b.md"]));
    expect(await screen.findByRole("tab", { name: "Document B" })).toBeInTheDocument();
    expect(await screen.findByRole("complementary", { name: "Side document: Document B" })).toBeInTheDocument();
    expect(localStorage.getItem(READING_SESSION_PREFIX + "reader")).not.toContain("Stale secret title");
  });

  it("keeps the side document while the primary tab changes and exposes an accessible splitter", async () => {
    const user = userEvent.setup(); setup(); await screen.findByRole("tab", { name: "Document A" });
    await user.click(screen.getByRole("link", { name: "Reference B" }));
    await user.click(await screen.findByRole("button", { name: "Keep side by side" }));
    const side = await screen.findByRole("complementary", { name: "Side document: Document B" });
    fireEvent.click(within(side).getByRole("link", { name: "Open as main document" }), { ctrlKey: true });
    expect(screen.getByRole("complementary", { name: "Side document: Document B" })).toBe(side);
    const splitter = screen.getByRole("separator", { name: "Resize side document" });
    const before = Number(splitter.getAttribute("aria-valuenow"));
    fireEvent.keyDown(splitter, { key: "ArrowLeft" });
    expect(Number(splitter.getAttribute("aria-valuenow"))).toBeGreaterThan(before);
    await user.click(screen.getByRole("tab", { name: "Document B" }));
    expect(await screen.findByRole("complementary", { name: "Side document: Document B" })).toBe(side);
    expect(screen.getByTestId("location")).toHaveTextContent("/vault/alpha/doc/b.md");
    await user.click(screen.getByRole("button", { name: "Close side document" }));
    expect(screen.queryByRole("complementary", { name: "Side document: Document B" })).toBeNull();
    expect(screen.getByRole("tab", { name: "Document B" })).toBeInTheDocument();
  });

  it("removes denied saved tabs without exposing their old labels", async () => {
    const session = emptyReadingSession(); session.tabs = [{ vault: "alpha", path: "b.md", title: "Private saved title", href: "/vault/alpha/doc/b.md" }];
    writeReadingSession("reader", session);
    vi.mocked(getDocument).mockImplementation(async (_vault, path) => { if (path === "b.md") throw new ApiError("Denied", 403, {}); return docs[path]; });
    setup(); await screen.findByRole("tab", { name: "Document A" });
    await waitFor(() => expect(localStorage.getItem(READING_SESSION_PREFIX + "reader")).not.toContain("b.md"));
    expect(screen.queryByText("Private saved title")).toBeNull();
    expect(screen.queryByRole("tab", { name: "Document B" })).toBeNull();
  });

  it("bounds restoration even when the foreground document is already cached at the end", async () => {
    const session = emptyReadingSession();
    const paths = Array.from({ length: 8 }, (_, index) => `saved-${index}.md`);
    session.tabs = [...paths, "a.md"].map(path => ({ vault: "alpha", path, title: "", href: `/vault/alpha/doc/${path}` }));
    writeReadingSession("reader", session);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(["document", "alpha", "a.md", undefined, "reader", 0], docs["a.md"]);
    const pending = new Map<string, (value: typeof docs["a.md"]) => void>();
    vi.mocked(getDocument).mockImplementation((_vault, path) => path === "a.md" ? Promise.resolve(docs[path]) : new Promise(resolve => { pending.set(path, resolve); }));
    setup({ client });
    await screen.findByRole("tab", { name: "Document A" });
    expect([...pending.keys()]).toEqual(paths.slice(0, 4));
    await act(async () => pending.get(paths[1])!({ ...docs["a.md"], path: paths[1], title: "Saved second" }));
    expect([...pending.keys()]).toEqual(paths.slice(0, 4));
    await act(async () => pending.get(paths[0])!({ ...docs["a.md"], path: paths[0], title: "Saved first" }));
    await waitFor(() => expect([...pending.keys()]).toEqual(paths.slice(0, 6)));
  });

});
