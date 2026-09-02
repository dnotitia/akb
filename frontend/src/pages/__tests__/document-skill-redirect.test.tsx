import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { VaultRefreshProvider } from "@/contexts/vault-refresh-context";
import DocumentPage from "@/pages/document";
import { VAULT_SKILL_PATH } from "@/lib/skill";

vi.mock("@/lib/api", () => ({
  authenticatedFetch: vi.fn(),
  getDocument: vi.fn(),
  getDocumentHistoryWithFallback: vi.fn(),
  getVaultInfo: vi.fn(),
  getRelations: vi.fn(),
  deleteDocument: vi.fn(),
  unpublishDoc: vi.fn(),
  updateDocument: vi.fn(),
}));

import {
  authenticatedFetch,
  getDocument,
  getDocumentHistoryWithFallback,
  getRelations,
  getVaultInfo,
} from "@/lib/api";

const authenticatedFetchMock = authenticatedFetch as unknown as ReturnType<typeof vi.fn>;
const getDocumentMock = getDocument as unknown as ReturnType<typeof vi.fn>;
const getDocumentHistoryMock = getDocumentHistoryWithFallback as unknown as ReturnType<typeof vi.fn>;
const getRelationsMock = getRelations as unknown as ReturnType<typeof vi.fn>;
const getVaultInfoMock = getVaultInfo as unknown as ReturnType<typeof vi.fn>;

const COMMIT = "abcdef1234567"; // pragma: allowlist secret — synthetic Git commit

function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="location">{`${loc.pathname}${loc.search}${loc.hash}`}</div>;
}

function renderAt(url: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[url]}>
        <VaultRefreshProvider refetchVaults={vi.fn()} refetchTree={vi.fn()}>
          <Routes>
            <Route path="/vault/:name/doc/:id" element={<DocumentPage />} />
            <Route path="/vault/:name/settings" element={<div>Vault settings</div>} />
          </Routes>
        </VaultRefreshProvider>
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  authenticatedFetchMock.mockReset();
  getDocumentMock.mockReset();
  getDocumentHistoryMock.mockReset();
  getRelationsMock.mockReset();
  getVaultInfoMock.mockReset();

  authenticatedFetchMock.mockResolvedValue({ ok: true, json: async () => ({ activity: [] }) });
  getDocumentHistoryMock.mockResolvedValue({
    kind: "document_history",
    source: "document",
    history: [],
  });
  getDocumentMock.mockResolvedValue({
    path: VAULT_SKILL_PATH,
    title: "Vault guide",
    content: "# Vault guide",
    current_commit: COMMIT,
    type: "skill",
    tags: [],
    is_public: false,
  });
  getRelationsMock.mockResolvedValue({ relations: [] });
  getVaultInfoMock.mockResolvedValue({ role: "owner" });
});

afterEach(cleanup);

describe("canonical vault-guide routing", () => {
  it("redirects the plain viewer to the settings guide editor", async () => {
    renderAt(`/vault/v/doc/${encodeURIComponent(VAULT_SKILL_PATH)}`);
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent("/vault/v/settings#skill"),
    );
    expect(screen.queryByRole("heading", { level: 1, name: "Vault guide" })).not.toBeInTheDocument();
  });

  it("keeps a ?commit= pinned version readable in the viewer", async () => {
    renderAt(`/vault/v/doc/${encodeURIComponent(VAULT_SKILL_PATH)}?commit=${COMMIT}`);
    expect(
      await screen.findByRole("heading", { level: 1, name: "Vault guide" }),
    ).toBeInTheDocument();
  });

  it("leaves ordinary documents on the viewer", async () => {
    getDocumentMock.mockResolvedValue({
      path: "notes/hello.md",
      title: "DocTitle",
      content: "# BodyHeading",
      current_commit: COMMIT,
      type: "note",
      tags: [],
      is_public: false,
    });
    renderAt("/vault/v/doc/notes%2Fhello.md");
    expect(
      await screen.findByRole("heading", { level: 1, name: "DocTitle" }),
    ).toBeInTheDocument();
  });
});
