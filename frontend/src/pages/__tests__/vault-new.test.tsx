import { cleanup, render, screen, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

// The Template control is now a themed dropdown (Radix DropdownMenu), not a
// native <select>, so its options live in a popover that we open before
// asserting / selecting.
async function openTemplateMenu() {
  const user = userEvent.setup();
  await user.click(await screen.findByLabelText(/template/i));
  return user;
}

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
    await openTemplateMenu();
    await waitFor(() =>
      expect(screen.getAllByRole("menuitemradio")).toHaveLength(3),
    );
    const labels = screen.getAllByRole("menuitemradio").map((i) => i.textContent || "");
    expect(labels[0]).toMatch(/none/i);
    expect(labels.join("|")).toMatch(/Engineering/);
    expect(labels.join("|")).toMatch(/QA/);
  });

  it("shows preview when a template is selected", async () => {
    (api.listVaultTemplates as any).mockResolvedValue(SAMPLE);
    renderPage();
    const user = await openTemplateMenu();
    await user.click(await screen.findByRole("menuitemradio", { name: /Engineering/i }));
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
    const user = await openTemplateMenu();
    await user.click(await screen.findByRole("menuitemradio", { name: /QA/i }));
    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "qvault" } });
    fireEvent.click(screen.getByRole("button", { name: /create vault/i }));
    await waitFor(() =>
      expect(api.createVault).toHaveBeenCalledWith("qvault", undefined, "qa"),
    );
  });

  it("falls back to 'None' only when listVaultTemplates rejects", async () => {
    (api.listVaultTemplates as any).mockRejectedValue(new Error("boom"));
    renderPage();
    await openTemplateMenu();
    await waitFor(() => expect(api.listVaultTemplates).toHaveBeenCalled());
    const items = screen.getAllByRole("menuitemradio");
    expect(items).toHaveLength(1);
    expect(items[0].textContent).toMatch(/none/i);
  });
});
