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
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";

import PublicationsPage from "@/pages/publications";
import { CurrentUserProvider } from "@/contexts/current-user-context";

vi.mock("@/lib/api", () => ({
  listPublications: vi.fn(),
  deletePublication: vi.fn(),
  getDocument: vi.fn(),
  getVaultInfo: vi.fn(),
}));

import { deletePublication, getDocument, getVaultInfo, listPublications } from "@/lib/api";

const listPublicationsMock = listPublications as unknown as ReturnType<
  typeof vi.fn
>;
const deletePublicationMock = deletePublication as unknown as ReturnType<
  typeof vi.fn
>;

const PUBLICATION = {
  slug: "runbook-public",
  share_url: "https://example.test/p/runbook-public",
  resource_type: "document",
  resource_uri: "akb://platform-docs/coll/guides/doc/runbook.md",
  vault: "platform-docs",
  title: "Incident runbook",
  mode: "live",
  expires_at: null,
  max_views: null,
  view_count: 12,
  allow_embed: false,
  section_filter: null,
  password_protected: true,
  created_at: "2026-08-20T00:00:00Z",
  snapshot_at: null,
} as const;

function pageElement({ account = "account-a", checking = false, revision = 0 } = {}) {
  return (
    <CurrentUserProvider user={{ user_id: account } as never} checking={checking} revision={revision}>
    <MemoryRouter initialEntries={["/vault/platform-docs/publications"]}>
      <Link to="/vault/other-vault/publications">Switch Vault</Link>
      <Routes>
        <Route
          path="/vault/:name/publications"
          element={<PublicationsPage />}
        />
      </Routes>
    </MemoryRouter>
    </CurrentUserProvider>
  );
}

function renderPage() { return render(pageElement()); }

beforeEach(() => {
  vi.clearAllMocks();
  deletePublicationMock.mockResolvedValue({ deleted: 1 });
  vi.mocked(getVaultInfo).mockResolvedValue({ role: "writer", is_archived: false, is_external_git: false });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("Public links page redesign", () => {
  it("keeps an empty vault focused without a forced context rail", async () => {
    listPublicationsMock.mockResolvedValue({ publications: [] });
    renderPage();

    expect(await screen.findByText("No public links yet")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 1, name: "Public links" }),
    ).toHaveClass("sr-only");
    expect(screen.getByRole("heading", { level: 2, name: "Public links" })).toBeInTheDocument();
    expect(document.title).toBe("platform-docs · Public links · AKB");
    expect(screen.getByTestId("publication-workspace")).toHaveClass(
      "w-full",
      "mx-auto",
      "max-w-7xl",
    );
    expect(screen.getByTestId("publication-ledger-header")).toHaveClass(
      "border-border",
      "pb-3",
    );
    expect(screen.getByTestId("publication-ledger-header")).not.toHaveClass(
      "bg-surface-2/55",
    );
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
    const policy = screen.getByTestId("publication-policy-summary");
    expect(within(policy).getByText("Public")).toBeInTheDocument();
    expect(within(policy).getByText("Read-only")).toBeInTheDocument();
    expect(within(policy).getByText("No sign-in required")).toBeInTheDocument();
    expect(policy).toHaveTextContent(/Public.*Read-only.*No sign-in required/);
    expect(screen.getByText("0 links")).toBeInTheDocument();
    expect(screen.getByTestId("publication-empty-state")).not.toHaveClass("min-h-80");
    expect(screen.getByText("Open a document, table, or file and choose Publish to create a public link.")).toBeInTheDocument();
    expect(screen.queryByText("Vault content is private by default")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Publish" })).not.toBeInTheDocument();
    const browseLink = screen.getByRole("link", {
      name: /Browse vault content/i,
    });
    expect(browseLink).toHaveAttribute("href", "/vault/platform-docs");
  });

  it("keeps filters out of an eight-link registry", async () => {
    listPublicationsMock.mockResolvedValue({
      publications: Array.from({ length: 8 }, (_, index) => ({
        ...PUBLICATION,
        slug: `public-${index}`,
        title: `Document ${index + 1}`,
      })),
    });
    renderPage();

    const ledger = await screen.findByRole("list", { name: "Public links" });
    expect(within(ledger).getAllByRole("listitem")).toHaveLength(8);
    expect(screen.queryByTestId("publication-controls")).not.toBeInTheDocument();
  });

  it("reveals scalable search and type filters only for a larger registry", async () => {
    const user = userEvent.setup();
    const publications = Array.from({ length: 9 }, (_, index) => ({
      ...PUBLICATION,
      slug: `published-${index}`,
      share_url: `https://example.test/p/published-${index}`,
      title: index === 8 ? "Release bundle" : `Document ${index + 1}`,
      resource_type: index === 8 ? "file" : "document",
      resource_uri:
        index === 8
          ? "akb://platform-docs/file/release-bundle"
          : `akb://platform-docs/coll/guides/doc/document-${index + 1}.md`,
    }));
    listPublicationsMock.mockResolvedValue({ publications });
    renderPage();

    const search = await screen.findByPlaceholderText(
      "Filter public links…",
    );
    expect(screen.getByTestId("publication-controls")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Files" }));
    const ledger = screen.getByRole("list", { name: "Public links" });
    expect(within(ledger).getByText("Release bundle")).toBeInTheDocument();
    expect(within(ledger).getByRole("link", { name: "Release bundle" })).toHaveAttribute(
      "href", "/vault/platform-docs/file/release-bundle",
    );
    expect(within(ledger).getByTestId("publication-index")).toHaveTextContent(
      "9",
    );
    expect(within(ledger).queryByText("Document 1")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "All" }));
    await user.type(search, "Document 2");
    expect(within(ledger).getByText("Document 2")).toBeInTheDocument();
    expect(within(ledger).queryByText("Document 1")).not.toBeInTheDocument();
  });

  it("reveals thirty loaded links at a time and resets the display after filtering", async () => {
    const user = userEvent.setup();
    listPublicationsMock.mockResolvedValue({
      publications: Array.from({ length: 65 }, (_, index) => ({
        ...PUBLICATION,
        slug: `public-${index}`,
        title: index === 64 ? "Release bundle" : `Document ${index + 1}`,
        resource_type: index === 64 ? "file" : "document",
      })),
    });
    renderPage();

    const ledger = await screen.findByRole("list", { name: "Public links" });
    expect(within(ledger).getAllByRole("listitem")).toHaveLength(30);
    expect(screen.getByText("Showing 30 of 65 loaded links")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show more links (35 remaining)" }));
    expect(within(ledger).getAllByRole("listitem")).toHaveLength(60);

    await user.click(screen.getByRole("button", { name: "Files" }));
    expect(within(ledger).getAllByRole("listitem")).toHaveLength(1);
    expect(within(ledger).getByTestId("publication-index")).toHaveTextContent("65");
    expect(screen.getByText("Showing 1 of 1 matching link")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "All" }));
    expect(within(ledger).getAllByRole("listitem")).toHaveLength(30);
    await user.click(screen.getByRole("button", { name: "Show more links (35 remaining)" }));
    await user.click(screen.getByRole("button", { name: "Show more links (5 remaining)" }));
    expect(within(ledger).getAllByRole("listitem")).toHaveLength(65);
    expect(screen.getByText("Showing 65 of 65 loaded links")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Show more links/ })).not.toBeInTheDocument();
    expect(listPublicationsMock).toHaveBeenCalledTimes(1);
  });

  it("distinguishes no matching links from an empty registry and clears filters", async () => {
    const user = userEvent.setup();
    listPublicationsMock.mockResolvedValue({
      publications: Array.from({ length: 9 }, (_, index) => ({
        ...PUBLICATION,
        slug: `public-${index}`,
        title: `Document ${index + 1}`,
      })),
    });
    renderPage();

    await user.type(await screen.findByRole("searchbox", { name: "Filter public links" }), "No match");
    expect(screen.getByRole("heading", { name: "No matching public links" })).toBeInTheDocument();
    expect(screen.queryByTestId("publication-empty-state")).not.toBeInTheDocument();
    expect(screen.getByTestId("publication-policy-summary")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(within(screen.getByRole("list", { name: "Public links" })).getAllByRole("listitem")).toHaveLength(9);
    expect(screen.getByRole("searchbox", { name: "Filter public links" })).toHaveValue("");
  });

  it("retains the compact registry boundary across loading, failure, and retry", async () => {
    let rejectLoad!: (error: Error) => void;
    listPublicationsMock
      .mockReturnValueOnce(new Promise((_, reject) => { rejectLoad = reject; }))
      .mockResolvedValueOnce({ publications: [] });
    const user = userEvent.setup();
    renderPage();

    expect(screen.getByRole("status", { name: "Loading public links" })).toBeInTheDocument();
    expect(screen.queryByText("0 links")).not.toBeInTheDocument();
    expect(screen.queryByTestId("publication-empty-state")).not.toBeInTheDocument();
    await act(async () => { rejectLoad(new Error("HTTP 503: public links unavailable")); });
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't load public links");
    expect(screen.getByRole("alert")).toHaveTextContent("HTTP 503: public links unavailable");
    expect(screen.getByTestId("publication-policy-summary")).toBeInTheDocument();
    expect(screen.queryByTestId("publication-empty-state")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("No public links yet")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(listPublicationsMock).toHaveBeenCalledTimes(2);
  });

  it("presents public links as a manageable ledger", async () => {
    const user = userEvent.setup();
    const writeClipboard = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    listPublicationsMock.mockResolvedValue({ publications: [PUBLICATION] });
    renderPage();

    const ledger = await screen.findByRole("list", { name: "Public links" });
    expect(within(ledger).getByText("Incident runbook")).toBeInTheDocument();
    expect(within(ledger).getByTestId("publication-index")).toHaveTextContent(
      "1",
    );
    expect(within(ledger).getByText("Active")).toBeInTheDocument();
    expect(within(ledger).getByText("Delivery: Live")).toBeInTheDocument();
    expect(within(ledger).getByText("Document")).toBeInTheDocument();
    expect(within(ledger).getByText("Password")).toBeInTheDocument();
    expect(within(ledger).getByText("No expiry")).toBeInTheDocument();
    expect(screen.getByText("12 total views")).toBeInTheDocument();
    expect(
      within(ledger).getByRole("link", { name: "Incident runbook" }),
    ).toHaveAttribute("href", "/vault/platform-docs/doc/guides%2Frunbook.md");
    expect(
      within(ledger).getByRole("link", { name: /Open public page/i }),
    ).toHaveAttribute("href", PUBLICATION.share_url);
    expect(screen.getByRole("link", { name: "Browse vault" })).toHaveAttribute(
      "href",
      "/vault/platform-docs",
    );

    await user.click(within(ledger).getByRole("button", { name: "Copy public link for Incident runbook" }));
    expect(writeClipboard).toHaveBeenCalledWith(PUBLICATION.share_url);
    expect(within(ledger).getByRole("button", { name: "Public link copied" })).toBeInTheDocument();

    await user.click(
      within(ledger).getByRole("button", { name: /More actions/i }),
    );
    await user.click(screen.getByRole("menuitem", { name: "Unpublish" }));
    expect(
      await screen.findByText('Unpublish "Incident runbook"?'),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Unpublish" }));
    await waitFor(() => {
      expect(deletePublicationMock).toHaveBeenCalledWith(
        "platform-docs",
        "runbook-public",
      );
    });
  });

  it("returns bounded table publications to their source table", async () => {
    listPublicationsMock.mockResolvedValue({
      publications: [
        {
          ...PUBLICATION,
          slug: "release-table",
          share_url: "https://example.test/p/release-table",
          resource_type: "table_query",
          resource_uri: null,
          title: "Release readiness",
          query_sql: "SELECT service, ready FROM release_status ORDER BY service LIMIT 100",
          query_vault_names: ["platform-docs"],
          query_params: {},
        },
      ],
    });
    renderPage();

    expect(
      await screen.findByRole("link", { name: "Release readiness" }),
    ).toHaveAttribute(
      "href",
      "/vault/platform-docs/table/release_status",
    );
  });

  it.each([
    [{ expires_at: "2026-08-01T00:00:00Z" }, "Expired"],
    [{ max_views: 12 }, "View limit reached"],
    [{ mode: "snapshot", max_views: 12 }, "View limit reached"],
    [{ expires_at: undefined }, "Availability unknown"],
  ])("separates delivery from availability %j", async (limits, status) => {
    listPublicationsMock.mockResolvedValue({ publications: [{ ...PUBLICATION, ...limits }] });
    renderPage();
    const ledger = await screen.findByRole("list", { name: "Public links" });
    expect(within(ledger).getByText(status)).toBeInTheDocument();
    expect(within(ledger).queryByText("Active")).toBeNull();
    expect(within(ledger).getByText(`Delivery: ${"mode" in limits ? "Snapshot" : "Live"}`)).toBeInTheDocument();
    if ("expires_at" in limits && limits.expires_at === undefined) {
      expect(within(ledger).getByText("Expiry unavailable")).toBeInTheDocument();
      expect(within(ledger).queryByText("No expiry")).toBeNull();
    }
  });

  it.each([
    [{ role: "reader" }, "Unpublish requires a Writer, Admin, or Owner role."],
    [{ role: "owner", is_archived: true }, "Vault 'platform-docs' is archived (read-only)"],
    [{ role: "owner", is_external_git: true }, "Vault 'platform-docs' is a read-only external git mirror"],
  ])("prevents unavailable Unpublish before confirmation %j", async (info, reason) => {
    vi.mocked(getVaultInfo).mockResolvedValue(info);
    listPublicationsMock.mockResolvedValue({ publications: [PUBLICATION] });
    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByText(reason)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Copy public link/ })).toBeEnabled();
    expect(screen.getByRole("link", { name: /Open public page/ })).toHaveAttribute("href", PUBLICATION.share_url);
    await user.click(screen.getByRole("button", { name: /More actions/ }));
    const item = screen.getByRole("menuitem", { name: "Unpublish" });
    expect(item).toHaveAttribute("aria-disabled", "true");
    await user.click(item);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(deletePublication).not.toHaveBeenCalled();
  });

  it("keeps read/copy access when permissions cannot be verified", async () => {
    vi.mocked(getVaultInfo).mockRejectedValue(new Error("Unavailable"));
    listPublicationsMock.mockResolvedValue({ publications: [PUBLICATION] });
    renderPage();
    expect(await screen.findByText("Unpublish is unavailable until Vault permissions can be verified.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Open public page/ })).toBeInTheDocument();
  });

  it.each(["Vault", "account", "revision"])("ignores late links after the %s boundary changes", async (boundary) => {
    let resolveOld!: (value: unknown) => void;
    listPublicationsMock.mockReturnValueOnce(new Promise((resolve) => { resolveOld = resolve; }));
    listPublicationsMock.mockResolvedValue({ publications: [{ ...PUBLICATION, slug: "new-link", title: "New registry" }] });
    const user = userEvent.setup();
    const page = renderPage();
    await waitFor(() => expect(listPublications).toHaveBeenCalledTimes(1));
    if (boundary === "Vault") await user.click(screen.getByRole("link", { name: "Switch Vault" }));
    else page.rerender(pageElement(boundary === "account" ? { account: "account-b" } : { revision: 1 }));
    await screen.findByText("New registry");
    await act(async () => { resolveOld({ publications: [PUBLICATION] }); });
    expect(screen.queryByText("Incident runbook")).toBeNull();
    expect(screen.getByText("New registry")).toBeInTheDocument();
  });

  it("ignores a late document-title enrichment after switching Vault", async () => {
    let resolveTitle!: (value: unknown) => void;
    listPublicationsMock.mockResolvedValueOnce({ publications: [{ ...PUBLICATION, title: null }] });
    vi.mocked(getDocument).mockReturnValueOnce(new Promise((resolve) => { resolveTitle = resolve; }) as never);
    listPublicationsMock.mockResolvedValue({ publications: [{ ...PUBLICATION, slug: "new-link", title: "New registry" }] });
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(getDocument).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("link", { name: "Switch Vault" }));
    await screen.findByText("New registry");
    await act(async () => { resolveTitle({ title: "Old private title" }); });
    expect(screen.queryByText("Old private title")).toBeNull();
    expect(screen.getByText("New registry")).toBeInTheDocument();
  });

  it("hides links and clears confirmations while verifying access", async () => {
    listPublicationsMock.mockResolvedValue({ publications: [PUBLICATION] });
    const user = userEvent.setup();
    const page = renderPage();
    await user.click(await screen.findByRole("button", { name: /More actions/ }));
    await user.click(screen.getByRole("menuitem", { name: "Unpublish" }));
    page.rerender(pageElement({ checking: true }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByText("Incident runbook")).toBeNull();
    expect(listPublications).toHaveBeenCalledTimes(1);
    vi.mocked(getVaultInfo).mockResolvedValue({ role: "reader" });
    page.rerender(pageElement({ revision: 1 }));
    expect(await screen.findByText("Unpublish requires a Writer, Admin, or Owner role.")).toBeInTheDocument();
    expect(listPublications).toHaveBeenCalledTimes(2);
  });

  it("updates availability when an open link reaches its expiry", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-14T00:00:00Z"));
    listPublicationsMock.mockResolvedValue({ publications: [{ ...PUBLICATION, expires_at: "2026-10-14T00:00:01Z" }] });
    await act(async () => { renderPage(); });
    expect(screen.getByText("Active")).toBeInTheDocument();
    await act(async () => { vi.advanceTimersByTime(1000); });
    expect(screen.getByText("Expired")).toBeInTheDocument();
    expect(screen.getByText("Delivery: Live")).toBeInTheDocument();
  });

  it("preserves the system-admin bypass for a managed Vault", async () => {
    vi.mocked(getVaultInfo).mockResolvedValue({ role: "admin", role_source: "system_admin", managed_by: "collector" });
    listPublicationsMock.mockResolvedValue({ publications: [PUBLICATION] });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: /More actions/ }));
    expect(screen.getByRole("menuitem", { name: "Unpublish" })).not.toHaveAttribute("aria-disabled", "true");
  });

  it("retains the registry and retries failed permissions before enabling Unpublish", async () => {
    let resolvePermissions!: (value: unknown) => void;
    vi.mocked(getVaultInfo)
      .mockRejectedValueOnce(new Error("HTTP 503: permissions temporarily unavailable"))
      .mockReturnValueOnce(new Promise((resolve) => { resolvePermissions = resolve; }));
    listPublicationsMock.mockResolvedValue({ publications: [PUBLICATION] });
    const user = userEvent.setup();
    renderPage();
    const ledger = await screen.findByRole("list", { name: "Public links" });
    expect(ledger).toHaveTextContent("Incident runbook");
    expect(screen.getByRole("alert")).toHaveTextContent("HTTP 503: permissions temporarily unavailable");
    await user.click(screen.getByRole("button", { name: /More actions/ }));
    expect(screen.getByRole("menuitem", { name: "Unpublish" })).toHaveAttribute("aria-disabled", "true");
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Retry permissions" }));
    expect(screen.getByRole("button", { name: "Retry permissions" })).toBeDisabled();
    expect(ledger).toHaveTextContent("Incident runbook");
    expect(listPublications).toHaveBeenCalledTimes(1);
    expect(getVaultInfo).toHaveBeenCalledTimes(2);
    await user.click(screen.getByRole("button", { name: /More actions/ }));
    expect(screen.getByRole("menuitem", { name: "Unpublish" })).toHaveAttribute("aria-disabled", "true");
    expect(deletePublication).not.toHaveBeenCalled();
    await act(async () => { resolvePermissions({ role: "writer" }); });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("menuitem", { name: "Unpublish" })).not.toHaveAttribute("aria-disabled", "true");
    await user.click(screen.getByRole("menuitem", { name: "Unpublish" }));
    await user.click(screen.getByRole("button", { name: "Unpublish" }));
    expect(deletePublication).toHaveBeenCalledWith("platform-docs", "runbook-public");
  });

  it("can retry permissions repeatedly without replacing the registry", async () => {
    vi.mocked(getVaultInfo)
      .mockRejectedValueOnce(new Error("HTTP 503"))
      .mockRejectedValueOnce(new Error("HTTP 503 again"))
      .mockResolvedValueOnce({ role: "reader" });
    listPublicationsMock.mockResolvedValue({ publications: [PUBLICATION] });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Retry permissions" }));
    await screen.findByText("HTTP 503 again");
    await user.click(screen.getByRole("button", { name: "Retry permissions" }));
    await screen.findByText("Unpublish requires a Writer, Admin, or Owner role.");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("list", { name: "Public links" })).toHaveTextContent("Incident runbook");
    expect(listPublications).toHaveBeenCalledTimes(1);
    expect(deletePublication).not.toHaveBeenCalled();
  });

  it.each(["Vault", "account", "revision"])("ignores late permission retry success across the %s boundary", async (boundary) => {
    let resolvePermissions!: (value: unknown) => void;
    vi.mocked(getVaultInfo)
      .mockRejectedValueOnce(new Error("HTTP 503"))
      .mockReturnValueOnce(new Promise((resolve) => { resolvePermissions = resolve; }))
      .mockResolvedValue({ role: "reader" });
    listPublicationsMock.mockResolvedValue({ publications: [PUBLICATION] });
    const user = userEvent.setup();
    const page = renderPage();
    await user.click(await screen.findByRole("button", { name: "Retry permissions" }));
    if (boundary === "Vault") await user.click(screen.getByRole("link", { name: "Switch Vault" }));
    else page.rerender(pageElement(boundary === "account" ? { account: "account-b" } : { revision: 1 }));
    await screen.findByText("Unpublish requires a Writer, Admin, or Owner role.");
    await act(async () => { resolvePermissions({ role: "owner" }); });
    expect(screen.getByText("Unpublish requires a Writer, Admin, or Owner role.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /More actions/ }));
    expect(screen.getByRole("menuitem", { name: "Unpublish" })).toHaveAttribute("aria-disabled", "true");
    expect(deletePublication).not.toHaveBeenCalled();
  });
});


it("shows the public-link ledger while optional titles are still loading", async () => {
  let resolveTitle!: (value: unknown) => void;
  vi.mocked(getDocument).mockReturnValueOnce(new Promise(resolve => { resolveTitle = resolve; }) as never);
  listPublicationsMock.mockResolvedValue({ publications: [{ ...PUBLICATION, title: null }, { ...PUBLICATION, slug: "ready", title: "Ready document" }] });
  renderPage();
  expect(await screen.findByRole("link", { name: "Ready document" })).toBeVisible();
  expect(screen.getByRole("list", { name: "Public links" }).children).toHaveLength(2);
  expect(screen.queryByRole("status", { name: "Loading public links" })).toBeNull();
  await act(async () => resolveTitle({ title: "Resolved title" }));
  expect(screen.getByRole("link", { name: "Resolved title" })).toBeVisible();
});
