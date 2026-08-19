import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SkillSection } from "../skill-section";

const getSkillTemplate = vi.fn();
const getVaultSkillPreview = vi.fn();
const updateDocument = vi.fn();

vi.mock("@/lib/api", () => ({
  getSkillTemplate: (...a: any[]) => getSkillTemplate(...a),
  getVaultSkillPreview: (...a: any[]) => getVaultSkillPreview(...a),
  updateDocument: (...a: any[]) => updateDocument(...a),
}));

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>
  );
}

const DOC = {
  content: "# my-v Guide\n\nStored body",
  updated_at: "2026-05-18T10:00:00Z",
  current_commit: "abc123def456",
};

beforeEach(() => {
  vi.clearAllMocks();
  getVaultSkillPreview.mockResolvedValue("composed agent text");
});

describe("SkillSection", () => {
  it("renders the editor surface for a normal vault with a guide", async () => {
    const u = userEvent.setup();
    render(wrap(<SkillSection vault="my-v" doc={DOC} canWrite />));

    // Anchor for the #skill redirect target.
    expect(document.getElementById("skill")).toBeTruthy();
    expect(screen.getByText(/Stored body/)).toBeTruthy();

    // History points at the canonical doc pinned to its commit — the one URL
    // the viewer does NOT bounce back to settings.
    const history = screen.getByRole("link", { name: /history/i });
    expect(history.getAttribute("href")).toBe(
      "/vault/my-v/doc/overview%2Fvault-skill.md?commit=abc123def456",
    );

    // Edit tab seeds from the doc body.
    await u.click(screen.getByRole("tab", { name: /^edit$/i }));
    expect(
      (screen.getByLabelText(/vault guide body/i) as HTMLTextAreaElement).value,
    ).toBe(DOC.content);
  });

  it("saves the edited body with a content-only PATCH", async () => {
    updateDocument.mockResolvedValue({ ok: true });
    const u = userEvent.setup();
    render(wrap(<SkillSection vault="my-v" doc={DOC} canWrite />));

    await u.click(screen.getByRole("tab", { name: /^edit$/i }));
    await u.type(screen.getByLabelText(/vault guide body/i), " more");
    await u.click(screen.getByRole("button", { name: /save guide/i }));

    await waitFor(() =>
      expect(updateDocument).toHaveBeenCalledWith("my-v", "overview/vault-skill.md", {
        content: `${DOC.content} more`,
      }),
    );
  });

  it("Reset opens the ConfirmDialog and writes the substituted template", async () => {
    getSkillTemplate.mockResolvedValue("# {vault} Vault Skill\n\nBody");
    updateDocument.mockResolvedValue({ ok: true });
    const u = userEvent.setup();
    render(wrap(<SkillSection vault="my-v" doc={DOC} canWrite />));

    await u.click(screen.getByRole("button", { name: /reset to template/i }));
    expect(await screen.findByText(/replace current content/i)).toBeTruthy();
    await u.click(screen.getByRole("button", { name: /^reset$/i }));

    await waitFor(() =>
      expect(updateDocument).toHaveBeenCalledWith("my-v", "overview/vault-skill.md", {
        content: "# my-v Vault Skill\n\nBody",
      }),
    );
  });

  it("hides Edit and Reset for a reader but keeps the read surfaces", () => {
    render(wrap(<SkillSection vault="my-v" doc={DOC} canWrite={false} />));

    expect(screen.queryByRole("tab", { name: /^edit$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /reset to template/i })).toBeNull();
    // Preview, Agent view and History are reads — a reader keeps them.
    expect(screen.getByText(/Stored body/)).toBeTruthy();
    expect(screen.getByRole("tab", { name: /agent view/i })).toBeTruthy();
    expect(screen.getByRole("link", { name: /history/i })).toBeTruthy();
  });

  it("renders the mirror note instead of the editor for an external-git vault", () => {
    render(wrap(<SkillSection vault="mirror-v" doc={null} isMirror canWrite />));
    expect(screen.getByText(/mirror vaults don't carry a vault guide/i)).toBeTruthy();
    expect(screen.queryByRole("tab", { name: /^edit$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /reset to template/i })).toBeNull();
  });

  it("renders the backfill note (and no create button) when the guide is missing", () => {
    render(wrap(<SkillSection vault="my-v" doc={null} canWrite />));
    expect(screen.getByText(/restored automatically by the system backfill/i)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /create/i })).toBeNull();
    expect(screen.queryByRole("tab", { name: /^edit$/i })).toBeNull();
  });
});
