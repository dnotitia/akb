import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { VaultRefreshProvider } from "@/contexts/vault-refresh-context";
import TablePage from "@/pages/table";

vi.mock("@/lib/api", () => ({
  createPublication: vi.fn(),
  createPublicationSnapshot: vi.fn(),
  deletePublication: vi.fn(),
  deleteVaultTable: vi.fn(),
  deleteVaultTableRow: vi.fn(),
  getVaultInfo: vi.fn(),
  insertVaultTableRow: vi.fn(),
  listPublications: vi.fn(),
  listVaultTableRows: vi.fn(),
  listVaultTables: vi.fn(),
  previewTablePublicationQuery: vi.fn(),
  updateVaultTableRow: vi.fn(),
}));

import {
  deleteVaultTableRow,
  getVaultInfo,
  insertVaultTableRow,
  listVaultTableRows,
  listVaultTables,
  updateVaultTableRow,
} from "@/lib/api";

const vaultInfoMock = getVaultInfo as unknown as ReturnType<typeof vi.fn>;
const listTablesMock = listVaultTables as unknown as ReturnType<typeof vi.fn>;
const listRowsMock = listVaultTableRows as unknown as ReturnType<typeof vi.fn>;
const insertRowMock = insertVaultTableRow as unknown as ReturnType<typeof vi.fn>;
const updateRowMock = updateVaultTableRow as unknown as ReturnType<typeof vi.fn>;
const deleteRowMock = deleteVaultTableRow as unknown as ReturnType<typeof vi.fn>;
const rowId = "6ab163e8-6ea4-4d20-8765-bf912716384c";

function renderTable(refetchTree = vi.fn()) {
  return {
    refetchTree,
    ...render(
      <MemoryRouter initialEntries={["/vault/ops/table/incidents"]}>
        <VaultRefreshProvider refetchVaults={vi.fn()} refetchTree={refetchTree}>
          <Routes>
            <Route path="/vault/:name/table/:table" element={<TablePage />} />
          </Routes>
        </VaultRefreshProvider>
      </MemoryRouter>,
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
    ));

    await user.click(screen.getByRole("button", { name: "Actions for row 1" }));
    await user.click(screen.getByRole("menuitem", { name: "Delete row" }));
    await user.click(screen.getByRole("button", { name: "Delete row" }));
    await waitFor(() => expect(deleteRowMock).toHaveBeenCalledWith("ops", "incidents", rowId));
  });

  it("keeps row mutation controls hidden for readers", async () => {
    vaultInfoMock.mockResolvedValue({ role: "reader", is_archived: false, is_external_git: false });
    renderTable();

    await screen.findByRole("heading", { name: "incidents" });
    await waitFor(() => expect(vaultInfoMock).toHaveBeenCalledWith("ops"));
    expect(screen.getByText("Read only")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add row" })).not.toBeInTheDocument();
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
});
