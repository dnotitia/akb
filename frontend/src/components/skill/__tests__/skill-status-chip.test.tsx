import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { SkillStatusChip } from "../skill-status-chip";

function wrap(ui: React.ReactNode) {
  return <MemoryRouter>{ui}</MemoryRouter>;
}

describe("SkillStatusChip", () => {
  it("renders defined variant with line count and link", () => {
    render(wrap(<SkillStatusChip vault="my-v" defined lineCount={142} />));
    expect(screen.getByText(/SKILL/)).toBeTruthy();
    expect(screen.getByText(/142L/)).toBeTruthy();
    const link = screen.getByRole("link");
    expect(link.getAttribute("href")).toContain("/vault/my-v/skill");
  });

  it("renders undefined variant", () => {
    render(wrap(<SkillStatusChip vault="my-v" defined={false} />));
    const t = screen.getByText(/SKILL/);
    expect(t.textContent).toContain("✗");
  });
});
