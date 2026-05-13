import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import VaultNewPage from "../vault-new";

vi.mock("@/lib/api", () => ({
  createVault: vi.fn(),
  listVaultTemplates: vi.fn().mockResolvedValue([]),
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function renderAtWithHistory(entries: string[]) {
  return render(
    <MemoryRouter initialEntries={entries} initialIndex={entries.length - 1}>
      <Routes>
        <Route path="/" element={<div data-testid="home" />} />
        <Route path="/vault/:name" element={<div data-testid="vault-page" />} />
        <Route path="/vault/new" element={<VaultNewPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

// JSDOM caveat: window.history.length is always 1 regardless of
// MemoryRouter's initialEntries. To exercise the "prior history" branch
// of handleCancel we stub history.length > 1 for those tests.
function stubHistoryLength(len: number) {
  vi.spyOn(window.history, "length", "get").mockReturnValue(len);
}

describe("VaultNewPage Cancel + ESC", () => {
  it("Cancel goes back when history has prior entry", () => {
    stubHistoryLength(2);
    renderAtWithHistory(["/vault/foo", "/vault/new"]);
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(screen.getByTestId("vault-page")).toBeInTheDocument();
  });

  it("Cancel falls back to / when no prior history", () => {
    stubHistoryLength(1);
    renderAtWithHistory(["/vault/new"]);
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(screen.getByTestId("home")).toBeInTheDocument();
  });

  it("ESC key triggers cancel", () => {
    stubHistoryLength(2);
    renderAtWithHistory(["/vault/foo", "/vault/new"]);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.getByTestId("vault-page")).toBeInTheDocument();
  });
});
