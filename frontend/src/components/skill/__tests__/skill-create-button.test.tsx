import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { SkillCreateButton } from "../skill-create-button";

const getSkillTemplate = vi.fn();
const putDocument = vi.fn();
const mockedNavigate = vi.fn();

vi.mock("@/lib/api", () => ({
  getSkillTemplate: (...a: any[]) => getSkillTemplate(...a),
  putDocument: (...a: any[]) => putDocument(...a),
}));

vi.mock("react-router-dom", async () => {
  const actual: any = await vi.importActual("react-router-dom");
  return { ...actual, useNavigate: () => mockedNavigate };
});

vi.mock("@tanstack/react-query", async () => {
  const actual: any = await vi.importActual("@tanstack/react-query");
  return {
    ...actual,
    useQueryClient: () => ({ invalidateQueries: vi.fn() }),
  };
});

beforeEach(() => {
  getSkillTemplate.mockReset();
  putDocument.mockReset();
  mockedNavigate.mockReset();
});

describe("SkillCreateButton", () => {
  it("fetches template, calls putDocument, navigates on success", async () => {
    getSkillTemplate.mockResolvedValue("# {vault} Vault Skill\n\nSeed body");
    putDocument.mockResolvedValue({ doc_id: "d-abc12345" });
    const u = userEvent.setup();

    render(
      <MemoryRouter initialEntries={["/vault/my-v/skill"]}>
        <Routes>
          <Route path="/vault/:name/skill" element={<SkillCreateButton vault="my-v" />} />
        </Routes>
      </MemoryRouter>,
    );

    await u.click(screen.getByRole("button", { name: /create from template/i }));

    await waitFor(() => {
      expect(getSkillTemplate).toHaveBeenCalledTimes(1);
      expect(putDocument).toHaveBeenCalledWith(
        expect.objectContaining({
          vault: "my-v",
          collection: "overview",
          title: "Vault Skill",
          type: "skill",
          content: expect.stringContaining("my-v Vault Skill"),
        }),
      );
    });
  });

  it("shows error if template fetch fails", async () => {
    getSkillTemplate.mockRejectedValue(new Error("network"));
    const u = userEvent.setup();
    render(<SkillCreateButton vault="my-v" />);
    await u.click(screen.getByRole("button", { name: /create from template/i }));
    await waitFor(() => expect(screen.getByText(/failed/i)).toBeTruthy());
  });
});
