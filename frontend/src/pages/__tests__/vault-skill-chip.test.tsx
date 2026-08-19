import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import VaultPage from "@/pages/vault";

vi.mock("@/lib/api", () => ({
  authenticatedFetch: vi.fn(),
  getDocument: vi.fn(),
  getRecent: vi.fn(),
  getSkillTemplate: vi.fn(),
  getVaultActivity: vi.fn(),
  getVaultInfo: vi.fn(),
}));

import {
  authenticatedFetch,
  getDocument,
  getRecent,
  getSkillTemplate,
  getVaultActivity,
  getVaultInfo,
} from "@/lib/api";

const authenticatedFetchMock = authenticatedFetch as unknown as ReturnType<typeof vi.fn>;
const getDocumentMock = getDocument as unknown as ReturnType<typeof vi.fn>;
const getRecentMock = getRecent as unknown as ReturnType<typeof vi.fn>;
const getSkillTemplateMock = getSkillTemplate as unknown as ReturnType<typeof vi.fn>;
const getVaultActivityMock = getVaultActivity as unknown as ReturnType<typeof vi.fn>;
const getVaultInfoMock = getVaultInfo as unknown as ReturnType<typeof vi.fn>;

// The endpoint serves the raw seed file (trailing newline intact); the stored
// body comes back frontmatter-parsed and whitespace-stripped. An untouched
// guide therefore differs from the template by exactly that newline.
const TEMPLATE = "# {vault} Guide\n\n(Describe what this vault is for.)\n";
const SEEDED_BODY = "# my-v Guide\n\n(Describe what this vault is for.)";

function renderVault() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/vault/my-v"]}>
        <Routes>
          <Route path="/vault/:name" element={<VaultPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const findChip = () => screen.findByRole("link", { name: "Open vault guide" });

beforeEach(() => {
  vi.clearAllMocks();
  authenticatedFetchMock.mockResolvedValue({ ok: false });
  getRecentMock.mockResolvedValue({ changes: [] });
  getVaultActivityMock.mockResolvedValue({ activity: [] });
  getSkillTemplateMock.mockResolvedValue(TEMPLATE);
  getDocumentMock.mockResolvedValue({ content: SEEDED_BODY });
  getVaultInfoMock.mockResolvedValue({
    name: "my-v",
    role: "owner",
    document_count: 4,
    table_count: 0,
    file_count: 0,
  });
});

afterEach(cleanup);

describe("vault page guide chip", () => {
  it("reads as 'template' when the body still matches the substituted seed", async () => {
    renderVault();
    const link = await findChip();
    await waitFor(() => expect(link.textContent).toContain("template"));
    expect(link.getAttribute("href")).toBe("/vault/my-v/settings#skill");
    expect(getSkillTemplateMock).toHaveBeenCalled();
  });

  it("reads as 'customized' once the body diverges", async () => {
    getDocumentMock.mockResolvedValue({
      content: `${SEEDED_BODY}\n\nWe keep incident write-ups here.`,
    });
    renderVault();
    const link = await findChip();
    await waitFor(() => expect(link.textContent).toContain("customized"));
    expect(link.getAttribute("href")).toBe("/vault/my-v/settings#skill");
  });

  it("shows no state (never a wrong one) while the template is in flight", async () => {
    getSkillTemplateMock.mockReturnValue(new Promise(() => {}));
    renderVault();
    const link = await findChip();
    await waitFor(() => expect(getSkillTemplateMock).toHaveBeenCalled());
    expect(link.textContent).not.toContain("customized");
    expect(link.textContent).not.toContain("template");
  });

  it("withholds the chip on a mirror vault, which carries no guide", async () => {
    getVaultInfoMock.mockResolvedValue({
      name: "mirror-v",
      role: "owner",
      is_external_git: true,
      document_count: 4,
      table_count: 0,
      file_count: 0,
    });
    getDocumentMock.mockRejectedValue(new Error("not found"));
    renderVault();
    // The stat tiles only render once /info resolved — i.e. once the chip
    // would have had everything it needs.
    await screen.findByText("Documents");
    expect(screen.queryByRole("link", { name: "Open vault guide" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Set up vault guide" })).toBeNull();
    expect(getSkillTemplateMock).not.toHaveBeenCalled();
  });

  it("points the empty-vault guide step at the settings editor", async () => {
    getVaultInfoMock.mockResolvedValue({
      name: "my-v",
      role: "owner",
      document_count: 1, // the seeded guide alone → still "empty"
      table_count: 0,
      file_count: 0,
    });
    renderVault();
    const step = await screen.findByRole("link", { name: /Edit the vault guide/ });
    expect(step.getAttribute("href")).toBe("/vault/my-v/settings#skill");
  });
});
