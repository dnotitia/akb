import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { VaultRefreshProvider } from "@/contexts/vault-refresh-context";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { ResourceLocationProvider, useResourceLocation } from "@/contexts/resource-location-context";
import TablePage from "@/pages/table";

vi.mock("@/lib/api", () => {
  class ApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.status = status;
    }
  }
  class TableRowConflictError extends Error {}
  return {
    ApiError,
    TableRowConflictError,
    createPublication: vi.fn(),
    createPublicationSnapshot: vi.fn(),
    deletePublication: vi.fn(),
    deleteVaultTable: vi.fn(),
    deleteVaultTableRow: vi.fn(),
    getVaultInfo: vi.fn(),
    getVaultTableRow: vi.fn(),
    insertVaultTableRow: vi.fn(),
    listPublications: vi.fn(),
    listVaultTableRows: vi.fn(),
    listVaultTables: vi.fn(),
    previewTablePublicationQuery: vi.fn(),
    updateVaultTableRow: vi.fn(),
  };
});

import {
  ApiError,
  createPublication,
  deleteVaultTableRow,
  getVaultTableRow,
  getVaultInfo,
  insertVaultTableRow,
  listVaultTableRows,
  listVaultTables,
  previewTablePublicationQuery,
  updateVaultTableRow,
  TableRowConflictError,
} from "@/lib/api";

const vaultInfoMock = getVaultInfo as unknown as ReturnType<typeof vi.fn>;
const listTablesMock = listVaultTables as unknown as ReturnType<typeof vi.fn>;
const listRowsMock = listVaultTableRows as unknown as ReturnType<typeof vi.fn>;
const insertRowMock = insertVaultTableRow as unknown as ReturnType<typeof vi.fn>;
const updateRowMock = updateVaultTableRow as unknown as ReturnType<typeof vi.fn>;
const deleteRowMock = deleteVaultTableRow as unknown as ReturnType<typeof vi.fn>;
const getRowMock = getVaultTableRow as unknown as ReturnType<typeof vi.fn>;
const rowId = "6ab163e8-6ea4-4d20-8765-bf912716384c";

function LocationProbe() {
  const location = useResourceLocation();
  const route = useLocation(), navigate = useNavigate();
  return <><output data-testid="resource-location">{location ? JSON.stringify(location) : "No resource"}</output><output data-testid="table-route">{route.pathname + route.search}</output><button onClick={() => navigate(-1)}>History Back</button><button onClick={() => navigate(1)}>History Forward</button></>;
}

function renderTable(
  refetchTree = vi.fn(),
  initialEntry = "/vault/ops/table/incidents",
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  const content = (revision = 0, checking = false) => (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/previous", initialEntry]} initialIndex={1}>
          <VaultRefreshProvider refetchVaults={vi.fn()} refetchTree={refetchTree}>
            <CurrentUserProvider user={null} revision={revision} checking={checking}>
              <ResourceLocationProvider identity="test-account" revision={revision} checking={checking}>
                <LocationProbe />
                <Routes>
                  <Route path="/vault/:name/table/:table" element={<TablePage />} />
                </Routes>
              </ResourceLocationProvider>
            </CurrentUserProvider>
          </VaultRefreshProvider>
        </MemoryRouter>
      </QueryClientProvider>
  );
  const view = render(content());
  return {
    refetchTree,
    queryClient,
    ...view,
    beginAccessCheck: () => view.rerender(content(0, true)),
    reverifyAccess: () => view.rerender(content(1)),
  };
}

beforeEach(() => {
  vaultInfoMock.mockReset();
  listTablesMock.mockReset();
  listRowsMock.mockReset();
  insertRowMock.mockReset();
  updateRowMock.mockReset();
  deleteRowMock.mockReset();
  getRowMock.mockReset();
  listTablesMock.mockResolvedValue({
    items: [{
      name: "incidents",
      collection: "operations",
      description: "Operational incidents",
      row_count: 1,
      columns: [
        { name: "title", type: "text", required: true },
        { name: "severity", type: "text" },
        { name: "score", type: "numeric" },
        { name: "metadata", type: "jsonb" },
      ],
    }],
  });
  listRowsMock.mockResolvedValue({
    kind: "table_query",
    columns: ["id", "title", "severity", "score", "metadata", "created_at"],
    items: [{
      id: rowId,
      title: "API outage",
      severity: "high",
      score: "0.74",
      metadata: { service: "gateway" },
      created_at: "2026-08-31T04:45:00Z",
      updated_at: "2026-09-01T04:45:00Z",
    }],
    total: 1,
  });
  insertRowMock.mockResolvedValue({ items: [], columns: [], total: 0 });
  updateRowMock.mockResolvedValue({ items: [], columns: [], total: 0 });
  deleteRowMock.mockResolvedValue({ items: [{ id: rowId }], columns: ["id"], total: 1 });
});

afterEach(() => cleanup());

describe("table row management", () => {
  it.each(["short", "x".repeat(240), { note: "json".repeat(100) }, null])("F22: Readers inspect full cell values by keyboard with one roving tab stop", async value => {
    vaultInfoMock.mockResolvedValue({ role: "reader" });
    listRowsMock.mockResolvedValue({ kind: "table_query", columns: ["title", "metadata"], items: [{ title: value, metadata: { ok: true } }], total: 1 });
    const actor = userEvent.setup(); const { container } = renderTable();
    const cell = await screen.findByRole("cell", { name: "Row 1, title. Press Enter to inspect full value." });
    expect(container.querySelectorAll('[data-cell-row][tabindex="0"]')).toHaveLength(1);
    cell.focus(); await actor.keyboard("{Enter}");
    const dialog = screen.getByRole("dialog", { name: "Cell value" });
    const full = value === null ? "null" : typeof value === "object" ? JSON.stringify(value) : value;
    expect(within(dialog).getByLabelText("title")).toHaveTextContent(full);
    const write = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    await actor.click(within(dialog).getByRole("button", { name: "Copy snippet" }));
    expect(write).toHaveBeenCalledWith(full);
    expect(within(dialog).getByRole("button", { name: "Snippet copied" })).toBeVisible();
    await actor.keyboard("{Escape}");
    await waitFor(() => expect(cell).toHaveFocus());
    await actor.keyboard("{ArrowRight}{Enter}");
    expect(within(screen.getByRole("dialog", { name: "Cell value" })).getByLabelText("metadata")).toHaveTextContent('{"ok":true}');
    expect(screen.queryByRole("button", { name: "Add row" })).not.toBeInTheDocument();
  });
  it("F23: user filters, page and sort create history while retaining unrelated query values", async () => {
    vaultInfoMock.mockResolvedValue({ role: "reader" });
    listRowsMock.mockResolvedValue({ columns: ["title", "severity"], items: Array.from({ length: 51 }, () => ({ title: "An incident", severity: "high" })), total: 51 });
    const actor = userEvent.setup(); renderTable(vi.fn(), "/vault/ops/table/incidents?external=keep");
    await actor.click(await screen.findByRole("button", { name: "Filters" }));
    const filter = screen.getByRole("dialog", { name: "Add a filter" });
    await actor.type(within(filter).getByLabelText("Value"), "incident");
    await actor.click(within(filter).getByRole("button", { name: "Apply filter" }));
    const filtered = screen.getByTestId("table-route").textContent;
    await actor.click(await screen.findByRole("button", { name: "Next page" }));
    await waitFor(() => expect(listRowsMock).toHaveBeenLastCalledWith("ops", "incidents", expect.objectContaining({ offset: 50 })));
    const paged = screen.getByTestId("table-route").textContent;
    await actor.click(screen.getByRole("button", { name: /Sort by title/ }));
    await waitFor(() => expect(listRowsMock).toHaveBeenLastCalledWith("ops", "incidents", expect.objectContaining({ order: "title.asc,id.asc", offset: 0 })));
    const sorted = screen.getByTestId("table-route").textContent;
    await actor.click(screen.getByRole("button", { name: "History Back" }));
    expect(screen.getByTestId("table-route")).toHaveTextContent(paged!);
    await actor.click(screen.getByRole("button", { name: "History Back" }));
    expect(screen.getByTestId("table-route")).toHaveTextContent(filtered!);
    await actor.click(screen.getByRole("button", { name: "History Forward" }));
    await actor.click(screen.getByRole("button", { name: "History Forward" }));
    expect(screen.getByTestId("table-route")).toHaveTextContent(sorted!);
    expect(screen.getByTestId("table-route")).toHaveTextContent("external=keep");
  });
  it("F23: a requested count corrects a confirmed empty out-of-range page by replacing its entry", async () => {
    vaultInfoMock.mockResolvedValue({ role: "reader" });
    listRowsMock.mockImplementation(async (_vault, _table, options) => ({
      columns: ["title"], items: options.countExact || options.offset ? [] : [{ title: "One" }],
      total: options.countExact || !options.offset ? 1 : 0,
    }));
    const actor = userEvent.setup(); renderTable(vi.fn(), "/vault/ops/table/incidents?page=99&external=keep");
    await actor.click(await screen.findByRole("button", { name: "Count rows" }));
    await waitFor(() => expect(listRowsMock).toHaveBeenLastCalledWith("ops", "incidents", expect.objectContaining({ offset: 0 })));
    expect(screen.getByTestId("table-route")).toHaveTextContent("external=keep");
    await actor.click(screen.getByRole("button", { name: "History Back" }));
    expect(screen.getByTestId("table-route")).toHaveTextContent("/previous");
  });
  it("does not count during discovery and counts the current view only on request", async () => {
    const one = { id: rowId, title: "API outage" };
    listRowsMock.mockImplementation(async (_vault, _table, options) => ({
      kind: "table_query", columns: ["id", "title"],
      items: options.countExact ? [] : Array.from({ length: 51 }, (_, i) => ({ ...one, id: `${rowId}-${i}` })),
      total: options.countExact ? 121 : 51,
    }));
    const user = userEvent.setup();
    renderTable();
    await screen.findByRole("button", { name: "Count rows" });
    expect(listTablesMock).toHaveBeenCalledWith("ops", { includeRowCounts: false });
    expect(listRowsMock).toHaveBeenCalledWith("ops", "incidents", expect.objectContaining({ limit: 51, offset: 0, countExact: false }));
    expect(screen.getAllByText("API outage")).toHaveLength(50);
    await user.click(screen.getByRole("button", { name: "Count rows" }));
    await screen.findByText("1–50 · 121 rows counted");
    expect(listRowsMock).toHaveBeenCalledWith("ops", "incidents", expect.objectContaining({ limit: 0, countExact: true, filters: [] }));
    await user.click(screen.getByRole("button", { name: "Next page" }));
    await screen.findByText("51–100 · 121 rows counted");
    expect(listRowsMock.mock.calls.filter((call) => call[2].countExact)).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Refresh records" }));
    await screen.findByText("51–100 · total not counted");
  });

  it("keeps browsing after a count failure and allows explicit retry", async () => {
    let counted = false;
    listRowsMock.mockImplementation(async (_vault, _table, options) => {
      if (options.countExact && !counted) { counted = true; throw new Error("count unavailable"); }
      return { kind: "table_query", columns: ["id", "title"], items: options.countExact ? [] : [{ id: rowId, title: "API outage" }], total: 1 };
    });
    const user = userEvent.setup();
    renderTable();
    await user.click(await screen.findByRole("button", { name: "Count rows" }));
    await screen.findByText(/Could not count rows/);
    expect(screen.getByText("API outage")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Count rows" }));
    await screen.findByText("1–1 · 1 row counted");
  });

  it.each([403, 404])("clears a denied count (%i) and does not revive it on transient retry", async status => {
    vaultInfoMock.mockResolvedValue({ role: "reader" });
    let counts = 0;
    listRowsMock.mockImplementation(async (_vault, _table, options) => {
      if (options.countExact) {
        counts += 1;
        if (counts === 2) throw new ApiError("Denied", status, {});
        if (counts === 3) throw new ApiError("Unavailable", 503, {});
      }
      return {
        kind: "table_query", columns: ["id", "title"],
        items: options.countExact ? [] : [{ id: rowId, title: "API outage" }],
        total: options.countExact ? (counts === 1 ? 121 : 122) : 1,
      };
    });
    const user = userEvent.setup();
    // Access is revoked remotely while this tab stays foreground: no revision
    // change or navigation can clear the prior successful count for us.
    renderTable();
    await user.click(await screen.findByRole("button", { name: "Count rows" }));
    await screen.findByText("1–1 · 121 rows counted");
    expect(screen.getByRole("button", { name: "Last counted page" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Recount rows" }));
    await screen.findByText(/Could not count rows/);
    await screen.findByText("1–1 · total not counted");
    expect(screen.queryByRole("button", { name: "Last counted page" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Count rows" }));
    await waitFor(() => expect(counts).toBe(3));
    expect(screen.getByText("1–1 · total not counted")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Last counted page" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Count rows" }));
    await screen.findByText("1–1 · 122 rows counted");
    expect(screen.getByRole("button", { name: "Last counted page" })).toBeInTheDocument();
  });

  it("does not bounce to an older counted page when another writer added rows", async () => {
    listRowsMock.mockImplementation(async (_vault, _table, options) => ({
      kind: "table_query", columns: ["id", "title"],
      items: options.countExact ? [] : Array.from({ length: 51 }, (_, i) => ({ id: `${rowId}-${i}`, title: `Row ${i}` })),
      total: options.countExact ? 50 : 51,
    }));
    const user = userEvent.setup();
    renderTable();
    await user.click(await screen.findByRole("button", { name: "Count rows" }));
    await screen.findByText("1–50 · 50 rows counted");
    await user.click(screen.getByRole("button", { name: "Next page" }));
    await screen.findByText("51–100 · 50 rows counted");
    expect(listRowsMock).toHaveBeenLastCalledWith("ops", "incidents", expect.objectContaining({ offset: 50, countExact: false }));
  });

  it.each(["checking", "failed", "catalog failure"])("blocks an open publication draft during %s access verification", async (state) => {
    vaultInfoMock.mockResolvedValue({ role: "writer", is_archived: false, is_external_git: false });
    vi.mocked(previewTablePublicationQuery).mockResolvedValue({ kind: "table_query", columns: ["title"], items: [{ title: "API outage" }], total: 1 });
    const user = userEvent.setup();
    const { beginAccessCheck, reverifyAccess } = renderTable();
    await user.click(await screen.findByRole("button", { name: "Actions for incidents" }));
    await user.click(screen.getByRole("menuitem", { name: "Publish table" }));
    const dialog = screen.getByRole("dialog", { name: "Publish table" });
    const title = within(dialog).getByLabelText("Public title");
    await user.clear(title);
    await user.type(title, "Incident summary");
    await user.click(within(dialog).getByRole("button", { name: "Preview query" }));
    const publish = within(dialog).getByRole("button", { name: "Publish live table" });
    await waitFor(() => expect(publish).toBeEnabled());
    beginAccessCheck();
    if (state !== "checking") {
      if (state === "failed") vaultInfoMock.mockRejectedValue(new Error("Access denied"));
      else listTablesMock.mockRejectedValue(new Error("Catalog unavailable"));
      reverifyAccess();
      await within(dialog).findByText(/could not be verified/);
    }
    expect(title).toHaveValue("Incident summary");
    expect(publish).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Refresh preview" })).toBeDisabled();
    // Submit-time validation must also reject keyboard/programmatic submission.
    fireEvent.submit(publish.closest("form")!);
    expect(createPublication).not.toHaveBeenCalled();
  });

  it("uses resolved location and one command row while preserving schema disclosure", async () => {
    vaultInfoMock.mockResolvedValue({ role: "writer", is_archived: false, is_external_git: false });
    const user = userEvent.setup();
    const { container } = renderTable();
    await screen.findByRole("button", { name: "Add row" });
    expect(screen.getByTestId("resource-location")).toHaveTextContent(JSON.stringify({
      vault: "ops", title: "incidents", kind: "Table", collectionPath: "operations", resourceRef: "incidents",
    }));
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1 })).toHaveClass("sr-only");
    expect(container.querySelectorAll('[data-slot="resource-command-row"]')).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Filters" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Schema" }));
    const schema = screen.getByRole("complementary", { name: "Table schema" });
    expect(within(schema).getByText("Operational incidents")).toBeInTheDocument();
    expect(within(schema).getByRole("heading", { name: "Column dictionary" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.getByRole("button", { name: "Schema" })).toHaveFocus());
  });

  it("does not publish a resolved table label when the catalog request fails", async () => {
    vaultInfoMock.mockResolvedValue({ role: "reader", is_archived: false, is_external_git: false });
    listTablesMock.mockRejectedValue(new Error("Access denied"));
    renderTable();
    await screen.findByRole("button", { name: "Schema" });
    expect(screen.getByTestId("resource-location")).toHaveTextContent("No resource");
    expect(screen.queryByRole("heading", { name: "incidents" })).not.toBeInTheDocument();
  });

  it("preserves an open row draft through same-account access revalidation", async () => {
    vaultInfoMock.mockResolvedValue({ role: "writer", is_archived: false, is_external_git: false });
    const user = userEvent.setup();
    const { beginAccessCheck, reverifyAccess } = renderTable();
    await user.click(await screen.findByRole("button", { name: "Add row" }));
    await user.type(screen.getByLabelText("title"), "Draft to retain");
    beginAccessCheck();
    expect(screen.getByTestId("resource-location")).toHaveTextContent("No resource");
    expect(screen.getByLabelText("title")).toHaveValue("Draft to retain");
    reverifyAccess();
    const dialog = await screen.findByRole("dialog", { name: "Add row" });
    expect(within(dialog).getByLabelText("title")).toHaveValue("Draft to retain");
    expect(insertRowMock).not.toHaveBeenCalled();
  });

  it("blocks a pending row deletion until foreground access has been verified", async () => {
    vaultInfoMock.mockResolvedValue({ role: "writer", is_archived: false, is_external_git: false });
    const user = userEvent.setup();
    const { beginAccessCheck } = renderTable();
    await user.click(await screen.findByRole("button", { name: "Actions for row 1" }));
    await user.click(screen.getByRole("menuitem", { name: "Delete row" }));
    beginAccessCheck();
    await user.click(screen.getByRole("button", { name: "Delete row" }));
    expect(await screen.findByText("Permissions are being refreshed. Row changes will be available once access is verified.", { selector: '[role="alert"] *' })).toBeInTheDocument();
    expect(deleteRowMock).not.toHaveBeenCalled();
  });

  it("keeps mutations unavailable after a failed permission refresh and supports retry", async () => {
    vaultInfoMock.mockResolvedValue({ role: "admin", is_archived: false, is_external_git: false });
    const user = userEvent.setup();
    const { beginAccessCheck, reverifyAccess } = renderTable();
    await waitFor(() => expect(screen.getByRole("button", { name: "Add row" })).toBeEnabled());
    beginAccessCheck();
    vaultInfoMock.mockRejectedValue(new Error("Access denied"));
    reverifyAccess();
    await screen.findByText("Permissions could not be verified. Retry before making changes.");
    expect(screen.getByRole("button", { name: "Add row" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Actions for row 1" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Actions for incidents/ })).not.toBeInTheDocument();
    vaultInfoMock.mockResolvedValue({ role: "writer", is_archived: false, is_external_git: false });
    await user.click(screen.getByRole("button", { name: "Retry permissions" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Add row" })).toBeEnabled());
  });

  it("retains a row draft without submitting it after permission refresh fails", async () => {
    vaultInfoMock.mockResolvedValue({ role: "writer", is_archived: false, is_external_git: false });
    const user = userEvent.setup();
    const { beginAccessCheck, reverifyAccess } = renderTable();
    await user.click(await screen.findByRole("button", { name: "Add row" }));
    await user.type(screen.getByLabelText("title"), "Draft to retain");
    beginAccessCheck();
    vaultInfoMock.mockRejectedValue(new Error("Access denied"));
    reverifyAccess();
    await screen.findByText("Permissions could not be verified. Retry before making changes.");
    const dialog = screen.getByRole("dialog", { name: "Add row" });
    await user.click(within(dialog).getByRole("button", { name: "Add row" }));
    await waitFor(() => expect(within(dialog).getByRole("alert")).toHaveTextContent("Permissions could not be verified"));
    expect(within(dialog).getByLabelText("title")).toHaveValue("Draft to retain");
    expect(insertRowMock).not.toHaveBeenCalled();
  });

  it("lets a writer add, edit, and delete a row through structured controls", async () => {
    vaultInfoMock.mockResolvedValue({ role: "writer", is_archived: false, is_external_git: false });
    const user = userEvent.setup();
    const { refetchTree } = renderTable();

    await user.click(await screen.findByRole("button", { name: "Add row" }));
    let dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText("title"), "Database pressure");
    await user.type(within(dialog).getByLabelText("severity"), "critical");
    await user.type(within(dialog).getByLabelText("score"), "0.87");
    fireEvent.change(within(dialog).getByLabelText("metadata"), {
      target: { value: '{"service":"postgres"}' },
    });
    await user.click(within(dialog).getByRole("button", { name: "Add row" }));

    await waitFor(() => expect(insertRowMock).toHaveBeenCalledWith("ops", "incidents", {
      title: "Database pressure",
      severity: "critical",
      score: "0.87",
      metadata: { service: "postgres" },
    }));
    expect(refetchTree).toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Actions for row 1" }));
    await user.click(screen.getByRole("menuitem", { name: "Edit row" }));
    dialog = screen.getByRole("dialog");
    const severity = within(dialog).getByLabelText("severity");
    await user.clear(severity);
    await user.type(severity, "critical");
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(updateRowMock).toHaveBeenCalledWith(
      "ops",
      "incidents",
      rowId,
      { severity: "critical" },
      { expectedUpdatedAt: "2026-09-01T04:45:00Z", force: false },
    ));

    await user.click(screen.getByRole("button", { name: "Actions for row 1" }));
    await user.click(screen.getByRole("menuitem", { name: "Delete row" }));
    await user.click(screen.getByRole("button", { name: "Delete row" }));
    await waitFor(() => expect(deleteRowMock).toHaveBeenCalledWith(
      "ops",
      "incidents",
      rowId,
      { expectedUpdatedAt: "2026-09-01T04:45:00Z" },
    ));
  });

  it("explains row write restrictions to readers", async () => {
    vaultInfoMock.mockResolvedValue({ role: "reader", is_archived: false, is_external_git: false });
    renderTable();

    await screen.findByRole("heading", { name: "incidents" });
    await waitFor(() => expect(vaultInfoMock).toHaveBeenCalledWith("ops"));
    expect(screen.getByText("Read only")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add row" })).toBeDisabled();
    expect(screen.getByText("Writer role required to add, edit, or delete rows.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Actions for row 1" })).not.toBeInTheDocument();
  });

  it("shows JSON validation before calling the row API", async () => {
    vaultInfoMock.mockResolvedValue({ role: "writer", is_archived: false, is_external_git: false });
    const user = userEvent.setup();
    renderTable();

    await user.click(await screen.findByRole("button", { name: "Add row" }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText("title"), "Broken payload");
    await user.type(within(dialog).getByLabelText("metadata"), "not-json");
    await user.click(within(dialog).getByRole("button", { name: "Add row" }));

    expect(await within(dialog).findByText("metadata must contain valid JSON.")).toBeInTheDocument();
    expect(insertRowMock).not.toHaveBeenCalled();
  });

  it("pages, sorts, and filters through server query options", async () => {
    vaultInfoMock.mockResolvedValue({ role: "writer", is_archived: false, is_external_git: false });
    listRowsMock.mockResolvedValue({
      kind: "table_query",
      columns: ["id", "title", "severity", "created_at", "updated_at"],
      items: Array.from({ length: 51 }, (_, index) => ({
        id: `${rowId}-${index}`,
        title: `API outage ${index}`,
        severity: "high",
        created_at: "2026-08-31T04:45:00Z",
        updated_at: "2026-09-01T04:45:00Z",
      })),
      total: 121,
    });
    const user = userEvent.setup();
    renderTable();

    await user.click(await screen.findByRole("button", { name: /Sort by title/ }));
    await waitFor(() => expect(listRowsMock).toHaveBeenLastCalledWith(
      "ops",
      "incidents",
      expect.objectContaining({ order: "title.asc,id.asc", offset: 0 }),
    ));

    await user.click(screen.getByRole("button", { name: "Filters" }));
    const dialog = screen.getByRole("dialog", { name: "Add a filter" });
    await user.click(within(dialog).getByLabelText("Column"));
    await user.click(screen.getByRole("menuitemradio", { name: /severity/ }));
    await user.type(within(dialog).getByLabelText("Value"), "high");
    await user.click(within(dialog).getByRole("button", { name: "Apply filter" }));
    await waitFor(() => expect(listRowsMock).toHaveBeenLastCalledWith(
      "ops",
      "incidents",
      expect.objectContaining({
        filters: [{ column: "severity", expression: "ilike.*high*" }],
        offset: 0,
      }),
    ));

    await user.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() => expect(listRowsMock).toHaveBeenLastCalledWith(
      "ops",
      "incidents",
      expect.objectContaining({ offset: 50, limit: 51, countExact: false }),
    ));
  });

  it("preserves an edit draft when a concurrent update is detected", async () => {
    vaultInfoMock.mockResolvedValue({ role: "writer", is_archived: false, is_external_git: false });
    updateRowMock.mockRejectedValueOnce(new TableRowConflictError(rowId, "update"));
    getRowMock.mockResolvedValue({
      id: rowId,
      title: "API outage",
      severity: "medium",
      score: "0.74",
      metadata: { service: "gateway" },
      updated_at: "2026-09-02T04:45:00Z",
    });
    const user = userEvent.setup();
    renderTable();

    await user.click(await screen.findByRole("button", { name: "Actions for row 1" }));
    await user.click(screen.getByRole("menuitem", { name: "Edit row" }));
    const dialog = screen.getByRole("dialog");
    const severity = within(dialog).getByLabelText("severity");
    await user.clear(severity);
    await user.type(severity, "critical");
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));

    expect(await within(dialog).findByText("This row has newer changes")).toBeInTheDocument();
    expect(severity).toHaveValue("critical");
    await user.click(within(dialog).getByRole("button", { name: "Reload current values" }));
    await waitFor(() => expect(getRowMock).toHaveBeenCalledWith("ops", "incidents", rowId));
    expect(within(dialog).getByLabelText("severity")).toHaveValue("medium");
  });

  it("requires an explicit overwrite after a concurrent edit", async () => {
    vaultInfoMock.mockResolvedValue({ role: "writer", is_archived: false, is_external_git: false });
    updateRowMock
      .mockRejectedValueOnce(new TableRowConflictError(rowId, "update"))
      .mockResolvedValueOnce({ items: [{ id: rowId }], columns: ["id"], total: 1 });
    const user = userEvent.setup();
    renderTable();

    await user.click(await screen.findByRole("button", { name: "Actions for row 1" }));
    await user.click(screen.getByRole("menuitem", { name: "Edit row" }));
    const dialog = screen.getByRole("dialog");
    const severity = within(dialog).getByLabelText("severity");
    await user.clear(severity);
    await user.type(severity, "critical");
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    const title = within(dialog).getByLabelText("title");
    await user.clear(title);
    await user.type(title, "API outage revised");
    await user.click(await within(dialog).findByRole("button", { name: "Overwrite anyway" }));

    await waitFor(() => expect(updateRowMock).toHaveBeenLastCalledWith(
      "ops",
      "incidents",
      rowId,
      { title: "API outage revised", severity: "critical" },
      { expectedUpdatedAt: "2026-09-01T04:45:00Z", force: true },
    ));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Edit row" })).not.toBeInTheDocument());
  });

  it("reloads a changed row before allowing deletion", async () => {
    vaultInfoMock.mockResolvedValue({ role: "writer", is_archived: false, is_external_git: false });
    deleteRowMock
      .mockRejectedValueOnce(new TableRowConflictError(rowId, "delete"))
      .mockResolvedValueOnce({ items: [{ id: rowId }], columns: ["id"], total: 1 });
    getRowMock.mockResolvedValue({
      id: rowId,
      title: "Renamed incident",
      updated_at: "2026-09-02T04:45:00Z",
    });
    const user = userEvent.setup();
    renderTable();

    await user.click(await screen.findByRole("button", { name: "Actions for row 1" }));
    await user.click(screen.getByRole("menuitem", { name: "Delete row" }));
    await user.click(screen.getByRole("button", { name: "Delete row" }));
    expect(await screen.findByText(/latest version is now loaded/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Delete row" }));

    await waitFor(() => expect(deleteRowMock).toHaveBeenLastCalledWith(
      "ops",
      "incidents",
      rowId,
      { expectedUpdatedAt: "2026-09-02T04:45:00Z" },
    ));
  });
});

it("shows records while optional schema discovery is pending", async () => {
  vaultInfoMock.mockResolvedValue({ role: "writer" });
  listTablesMock.mockReturnValue(new Promise(() => {}));
  renderTable();
  await screen.findByText("API outage");
  expect(screen.getByRole("button", { name: "Add row" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Schema" }));
  expect(await screen.findByRole("status", { name: "Loading table schema" })).toBeInTheDocument();
});
it("keeps records available and offers local schema retry after a catalog error", async () => {
  vaultInfoMock.mockResolvedValue({ role: "writer" });
  listTablesMock.mockRejectedValue(new Error("Catalog unavailable"));
  renderTable();
  await screen.findByText("API outage");
  fireEvent.click(screen.getByRole("button", { name: "Schema" }));
  expect(await screen.findByRole("button", { name: "Retry schema" })).toBeEnabled();
  expect(screen.queryByText("Column metadata was not included in the table catalog response.")).not.toBeInTheDocument();
});

it("does not resurrect denied schema after a transient retry", async () => {
  vaultInfoMock.mockResolvedValue({ role: "writer" });
  const { queryClient } = renderTable();
  await screen.findByText("API outage");
  fireEvent.click(screen.getByRole("button", { name: "Schema" }));
  await screen.findAllByText("Operational incidents");
  listTablesMock.mockRejectedValue(new ApiError("Denied", 403, {}));
  await queryClient.refetchQueries({ queryKey: ["vault-tables"] });
  await waitFor(() => expect(screen.queryByText("Operational incidents")).not.toBeInTheDocument());
  listTablesMock.mockRejectedValue(new ApiError("Unavailable", 503, {}));
  await queryClient.refetchQueries({ queryKey: ["vault-tables"] });
  expect(screen.queryByText("Operational incidents")).not.toBeInTheDocument();
  listTablesMock.mockResolvedValue({ items: [{ name: "incidents", description: "Fresh schema", columns: [{ name: "title", type: "text" }] }] });
  await queryClient.refetchQueries({ queryKey: ["vault-tables"] });
  expect((await screen.findAllByText("Fresh schema")).length).toBeGreaterThan(0);
});
