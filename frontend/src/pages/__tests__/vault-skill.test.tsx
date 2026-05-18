import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider, QueryCache } from "@tanstack/react-query";
import VaultSkillPage from "../vault-skill";

const getDocument = vi.fn();
vi.mock("@/lib/api", () => ({
  getDocument: (...a: any[]) => getDocument(...a),
  putDocument: vi.fn(),
  getSkillTemplate: vi.fn(),
}));

function wrap(initial: string) {
  // queryCache with silent onError prevents vitest from treating
  // TanStack Query's internal error propagation as an unhandled rejection.
  const qc = new QueryClient({
    queryCache: new QueryCache({ onError: () => {} }),
    defaultOptions: { queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initial]}>
        <Routes>
          <Route path="/vault/:name/skill" element={<VaultSkillPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("VaultSkillPage", () => {
  it("renders the doc body when vault-skill exists", async () => {
    getDocument.mockResolvedValue({
      doc_id: "d-abc",
      title: "my-v Vault Skill",
      type: "skill",
      content: "# my-v Vault Skill\n\nBody here",
      tags: ["akb:skill"],
    });
    render(wrap("/vault/my-v/skill"));
    expect(await screen.findByText(/my-v Vault Skill/)).toBeTruthy();
  });

  it("renders Create CTA when vault-skill is missing (404)", async () => {
    getDocument.mockImplementation(() => Promise.reject(new Error("404 Not Found")));
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    render(wrap("/vault/empty-v/skill"));
    expect(await screen.findByText(/No vault skill yet/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: /create from template/i })).toBeTruthy();
    spy.mockRestore();
  });
});
