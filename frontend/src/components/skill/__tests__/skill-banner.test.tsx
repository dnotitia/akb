import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SkillBanner } from "../skill-banner";

const getDocument = vi.fn();
const getSkillTemplate = vi.fn();
const updateDocument = vi.fn();

vi.mock("@/lib/api", () => ({
  getDocument: (...a: any[]) => getDocument(...a),
  getSkillTemplate: (...a: any[]) => getSkillTemplate(...a),
  updateDocument: (...a: any[]) => updateDocument(...a),
}));

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>;
}

beforeEach(() => {
  vi.clearAllMocks();
  // The banner self-fetches its doc (enabled) — default to a resolved doc so
  // the query never settles with `undefined` (which TanStack rejects).
  getDocument.mockResolvedValue({
    title: "my-v Guide",
    content: "# body",
    type: "skill",
    tags: ["akb:skill"],
  });
});

describe("SkillBanner", () => {
  it("renders SKILL badge + context line", () => {
    render(wrap(<SkillBanner vault="my-v" docId="overview/vault-skill.md" />));
    expect(screen.getByText(/Guide/)).toBeTruthy();
    expect(screen.getByText(/agents.*read this/i)).toBeTruthy();
  });

  it("self-fetches the doc so Edit details enables (regression: 3- vs 4-element key)", async () => {
    render(wrap(<SkillBanner vault="my-v" docId="overview/vault-skill.md" />));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /edit details/i }),
      ).not.toBeDisabled(),
    );
    expect(getDocument).toHaveBeenCalledWith("my-v", "overview/vault-skill.md");
  });

  it("Reset opens ConfirmDialog, confirm calls updateDocument(vault, docId, { content })", async () => {
    getSkillTemplate.mockResolvedValue("# {vault} Vault Skill\n\nBody");
    updateDocument.mockResolvedValue({ ok: true });
    const u = userEvent.setup();
    render(wrap(<SkillBanner vault="my-v" docId="overview/vault-skill.md" />));
    await u.click(screen.getByRole("button", { name: /reset to template/i }));
    expect(await screen.findByText(/replace current content/i)).toBeTruthy();
    await u.click(screen.getByRole("button", { name: /^reset$/i }));
    await waitFor(() => {
      expect(updateDocument).toHaveBeenCalledWith(
        "my-v",
        "overview/vault-skill.md",
        expect.objectContaining({
          content: expect.stringContaining("my-v Vault Skill"),
        }),
      );
    });
  });
});
