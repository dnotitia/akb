import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { VaultRefreshProvider } from "@/contexts/vault-refresh-context";
import FilePage from "@/pages/file";
import TablePage from "@/pages/table";

vi.mock("@/lib/api", () => ({
  authenticatedFetch: vi.fn(),
  createPublication: vi.fn(),
  createPublicationSnapshot: vi.fn(),
  deletePublication: vi.fn(),
  getVaultInfo: vi.fn(),
  getDocument: vi.fn(),
  listPublications: vi.fn(),
  previewTablePublicationQuery: vi.fn(),
  deleteVaultFile: vi.fn(),
  deleteVaultTable: vi.fn(),
  deleteVaultTableRow: vi.fn(),
  insertVaultTableRow: vi.fn(),
  listVaultTableRows: vi.fn(),
  listVaultTables: vi.fn(),
  updateVaultTableRow: vi.fn(),
}));

import {
  authenticatedFetch,
  createPublication,
  createPublicationSnapshot,
  deletePublication,
  deleteVaultFile,
  deleteVaultTable,
  getVaultInfo,
  listPublications,
  listVaultTableRows,
  listVaultTables,
  previewTablePublicationQuery,
} from "@/lib/api";

const fetchMock = authenticatedFetch as unknown as ReturnType<typeof vi.fn>;
const vaultInfoMock = getVaultInfo as unknown as ReturnType<typeof vi.fn>;
const deleteFileMock = deleteVaultFile as unknown as ReturnType<typeof vi.fn>;
const deleteTableMock = deleteVaultTable as unknown as ReturnType<typeof vi.fn>;
const createPublicationMock = createPublication as unknown as ReturnType<typeof vi.fn>;
const snapshotMock = createPublicationSnapshot as unknown as ReturnType<typeof vi.fn>;
const deletePublicationMock = deletePublication as unknown as ReturnType<typeof vi.fn>;
const listPublicationsMock = listPublications as unknown as ReturnType<typeof vi.fn>;
const previewPublicationMock = previewTablePublicationQuery as unknown as ReturnType<typeof vi.fn>;
const listTablesMock = listVaultTables as unknown as ReturnType<typeof vi.fn>;
const listRowsMock = listVaultTableRows as unknown as ReturnType<typeof vi.fn>;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function LocationProbe() {
  return <span data-testid="pathname">{useLocation().pathname}</span>;
}

function renderRoute(path: string, refetchTree = vi.fn()) {
  return {
    refetchTree,
    ...render(
      <MemoryRouter initialEntries={[path]}>
        <VaultRefreshProvider refetchVaults={vi.fn()} refetchTree={refetchTree}>
          <Routes>
            <Route path="/vault/:name/file/:id" element={<FilePage />} />
            <Route path="/vault/:name/table/:table" element={<TablePage />} />
            <Route path="/vault/:name" element={<div>Vault overview</div>} />
          </Routes>
          <LocationProbe />
        </VaultRefreshProvider>
      </MemoryRouter>,
    ),
  };
}

beforeEach(() => {
  fetchMock.mockReset();
  vaultInfoMock.mockReset();
  deleteFileMock.mockReset();
  deleteTableMock.mockReset();
  deleteFileMock.mockResolvedValue({ deleted: true });
  deleteTableMock.mockResolvedValue({ deleted: true });
  listPublicationsMock.mockResolvedValue({ publications: [] });
  createPublicationMock.mockResolvedValue({});
  snapshotMock.mockResolvedValue({});
  deletePublicationMock.mockResolvedValue({ deleted: 1 });
  previewPublicationMock.mockResolvedValue({ columns: [], items: [], total: 0 });
  listTablesMock.mockReset();
  listRowsMock.mockReset();
  listTablesMock.mockResolvedValue({
    items: [{
      name: "audit_log",
      row_count: 1,
      columns: [{ name: "event", type: "text" }],
    }],
  });
  listRowsMock.mockResolvedValue({
    kind: "table_query",
    items: [{ id: "6ab163e8-6ea4-4d20-8765-bf912716384c", event: "First" }],
    columns: ["id", "event"],
    total: 1,
  });
});

afterEach(() => cleanup());

describe("resource viewer deletion", () => {
  it("lets a writer delete a file, refreshes the tree, and leaves the stale viewer", async () => {
    vaultInfoMock.mockResolvedValue({ role: "writer" });
    fetchMock.mockImplementation((url: string) => {
      if (url.endsWith("/download")) {
        return Promise.resolve(jsonResponse({
          name: "diagram.png",
          download_url: "https://example.test/diagram.png",
          mime_type: "image/png",
        }));
      }
      return Promise.resolve(jsonResponse({
        items: [
          {
            uri: "akb://v/file/file-1",
            name: "diagram.png",
            mime_type: "image/png",
          },
        ],
      }));
    });
    const user = userEvent.setup();
    const { refetchTree } = renderRoute("/vault/v/file/file-1");

    await user.click(await screen.findByRole("button", { name: "Actions for diagram.png" }));
    expect(screen.getByRole("menuitem", { name: "Publish file" })).toBeInTheDocument();
    await user.click(screen.getByRole("menuitem", { name: "Delete file" }));
    await user.click(screen.getByRole("button", { name: "Delete file" }));

    await waitFor(() => expect(deleteFileMock).toHaveBeenCalledWith("v", "file-1"));
    expect(refetchTree).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("pathname")).toHaveTextContent("/vault/v");
  });

  it("does not expose file publication or deletion to a reader", async () => {
    vaultInfoMock.mockResolvedValue({ role: "reader" });
    fetchMock.mockImplementation((url: string) => {
      if (url.endsWith("/download")) {
        return Promise.resolve(jsonResponse({
          name: "diagram.png",
          download_url: "https://example.test/diagram.png",
          mime_type: "image/png",
        }));
      }
      return Promise.resolve(jsonResponse({
        items: [{
          uri: "akb://v/file/file-1",
          name: "diagram.png",
          mime_type: "image/png",
        }],
      }));
    });
    renderRoute("/vault/v/file/file-1");

    await screen.findByRole("heading", { name: "diagram.png" });
    await waitFor(() => expect(vaultInfoMock).toHaveBeenCalledWith("v"));
    expect(
      screen.queryByRole("button", { name: "Actions for diagram.png" }),
    ).not.toBeInTheDocument();
  });

  it("requires an admin and an exact name before deleting a table", async () => {
    vaultInfoMock.mockResolvedValue({ role: "admin" });
    const user = userEvent.setup();
    const { refetchTree } = renderRoute("/vault/v/table/audit_log");

    await user.click(await screen.findByRole("button", { name: "Actions for audit_log" }));
    await user.click(screen.getByRole("menuitem", { name: "Delete table" }));
    const confirm = screen.getByRole("button", { name: "Delete table" });
    expect(confirm).toBeDisabled();
    await user.type(screen.getByLabelText(/type the table name/i), "audit_log");
    await user.click(confirm);

    await waitFor(() => expect(deleteTableMock).toHaveBeenCalledWith("v", "audit_log"));
    expect(refetchTree).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("pathname")).toHaveTextContent("/vault/v");
  });

  it("lets a writer publish a table without exposing admin-only deletion", async () => {
    vaultInfoMock.mockResolvedValue({ role: "writer" });
    const user = userEvent.setup();
    renderRoute("/vault/v/table/audit_log");

    await screen.findByRole("heading", { name: "audit_log" });
    await user.click(screen.getByRole("button", { name: "Actions for audit_log" }));
    expect(screen.getByRole("menuitem", { name: "Publish table" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Delete table" })).not.toBeInTheDocument();
  });

  it("does not expose table publication or deletion to a reader", async () => {
    vaultInfoMock.mockResolvedValue({ role: "reader" });
    renderRoute("/vault/v/table/audit_log");

    await screen.findByRole("heading", { name: "audit_log" });
    await waitFor(() => expect(vaultInfoMock).toHaveBeenCalledWith("v"));
    expect(
      screen.queryByRole("button", { name: "Actions for audit_log" }),
    ).not.toBeInTheDocument();
  });
});
