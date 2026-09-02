import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { VaultRefreshProvider } from "@/contexts/vault-refresh-context";
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
  deleteVaultTableRow,
  getVaultTableRow,
  getVaultInfo,
  insertVaultTableRow,
  listVaultTableRows,
  listVaultTables,
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

function renderTable(
  refetchTree = vi.fn(),
  initialEntry = "/vault/ops/table/incidents",
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  return {
    refetchTree,
    ...render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[initialEntry]}>
          <VaultRefreshProvider refetchVaults={vi.fn()} refetchTree={refetchTree}>
            <Routes>
              <Route path="/vault/:name/table/:table" element={<TablePage />} />
            </Routes>
          </VaultRefreshProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    ),
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
      items: [{
        id: rowId,
        title: "API outage",
        severity: "high",
        created_at: "2026-08-31T04:45:00Z",
        updated_at: "2026-09-01T04:45:00Z",
      }],
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
      expect.objectContaining({ offset: 50, limit: 50 }),
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
    await user.click(await within(dialog).findByRole("button", { name: "Overwrite anyway" }));

    await waitFor(() => expect(updateRowMock).toHaveBeenLastCalledWith(
      "ops",
      "incidents",
      rowId,
      { severity: "critical" },
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
