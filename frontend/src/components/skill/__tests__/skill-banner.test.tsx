import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SkillBanner } from "../skill-banner";

const getSkillTemplate = vi.fn();
const updateDocument = vi.fn();

vi.mock("@/lib/api", () => ({
  getSkillTemplate: (...a: any[]) => getSkillTemplate(...a),
  updateDocument: (...a: any[]) => updateDocument(...a),
}));

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>;
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("SkillBanner", () => {
  it("renders SKILL badge + context line", () => {
    render(wrap(<SkillBanner vault="my-v" docId="overview/vault-skill.md" />));
    expect(screen.getByText(/SKILL/)).toBeTruthy();
    expect(screen.getByText(/agents.*read this/i)).toBeTruthy();
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
