import { cleanup, render, screen, fireEvent, waitFor } from "@testing-library/react";
import { afterEach, describe, it, expect, vi, beforeEach } from "vitest";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import VaultNewPage from "../vault-new";
import * as api from "@/lib/api";

vi.mock("@/lib/api", () => ({
  listVaultTemplates: vi.fn(),
  createVault: vi.fn(),
}));

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/vault/new"]}>
      <Routes>
        <Route path="/vault/new" element={<VaultNewPage />} />
        <Route path="/vault/:name" element={<div data-testid="vault-landed" />} />
      </Routes>
    </MemoryRouter>,
  );
}

const SAMPLE = [
  {
    name: "engineering", display_name: "Engineering",
    description: "Software dev",
    collection_count: 2,
    collections: [{ path: "specs", name: "Specs" }, { path: "decisions", name: "Decisions" }],
  },
  {
    name: "qa", display_name: "QA",
    description: "Quality assurance",
    collection_count: 1,
    collections: [{ path: "test-plans", name: "Test plans" }],
  },
];

describe("VaultNewPage template selection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.createVault as any).mockResolvedValue({ vault_id: "v1", name: "x" });
  });

  afterEach(() => {
    cleanup();
  });

  it("renders dropdown with 'None' + fetched templates", async () => {
    (api.listVaultTemplates as any).mockResolvedValue(SAMPLE);
    renderPage();
    const select = await screen.findByLabelText(/template/i);
    await waitFor(() => expect((select as HTMLSelectElement).options.length).toBe(3));
    const labels = Array.from((select as HTMLSelectElement).options).map((o) => o.text);
    expect(labels[0]).toMatch(/none/i);
    expect(labels).toContain("Engineering");
    expect(labels).toContain("QA");
  });

  it("shows preview when a template is selected", async () => {
    (api.listVaultTemplates as any).mockResolvedValue(SAMPLE);
    renderPage();
    const select = (await screen.findByLabelText(/template/i)) as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBe(3));
    fireEvent.change(select, { target: { value: "engineering" } });
    expect(await screen.findByText(/software dev/i)).toBeInTheDocument();
    expect(screen.getByText(/specs/)).toBeInTheDocument();
    expect(screen.getByText(/decisions/)).toBeInTheDocument();
  });

  it("hides preview for 'None'", async () => {
    (api.listVaultTemplates as any).mockResolvedValue(SAMPLE);
    renderPage();
    await waitFor(() => expect(api.listVaultTemplates).toHaveBeenCalled());
    expect(screen.queryByText(/software dev/i)).not.toBeInTheDocument();
  });

  it("submits with undefined template when 'None' selected", async () => {
    (api.listVaultTemplates as any).mockResolvedValue(SAMPLE);
    renderPage();
    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "mvault" } });
    fireEvent.click(screen.getByRole("button", { name: /create vault/i }));
    await waitFor(() =>
      expect(api.createVault).toHaveBeenCalledWith("mvault", undefined, undefined),
    );
  });

  it("submits with selected template name", async () => {
    (api.listVaultTemplates as any).mockResolvedValue(SAMPLE);
    renderPage();
    const select = (await screen.findByLabelText(/template/i)) as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBe(3));
    fireEvent.change(select, { target: { value: "qa" } });
    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "qvault" } });
    fireEvent.click(screen.getByRole("button", { name: /create vault/i }));
    await waitFor(() =>
      expect(api.createVault).toHaveBeenCalledWith("qvault", undefined, "qa"),
    );
  });

  it("falls back to 'None' only when listVaultTemplates rejects", async () => {
    (api.listVaultTemplates as any).mockRejectedValue(new Error("boom"));
    renderPage();
    const select = (await screen.findByLabelText(/template/i)) as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBe(1));
    expect(select.options[0].text).toMatch(/none/i);
  });
});
