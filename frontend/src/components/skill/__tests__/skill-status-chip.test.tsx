import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { SkillStatusChip } from "../skill-status-chip";

function wrap(ui: React.ReactNode) {
  return <MemoryRouter>{ui}</MemoryRouter>;
}

describe("SkillStatusChip", () => {
  it("defined → links to the underlying guide doc", () => {
    render(wrap(<SkillStatusChip vault="my-v" defined lineCount={142} />));
    expect(screen.getByText(/Guide/)).toBeTruthy();
    expect(screen.getByText(/142L/)).toBeTruthy();
    const link = screen.getByRole("link");
    expect(link.getAttribute("href")).toBe(
      "/vault/my-v/doc/overview%2Fvault-skill.md",
    );
  });

  it("undefined → links to vault settings", () => {
    render(wrap(<SkillStatusChip vault="my-v" defined={false} />));
    const t = screen.getByText(/Guide/);
    expect(t.textContent).toContain("✗");
    expect(screen.getByRole("link").getAttribute("href")).toBe(
      "/vault/my-v/settings",
    );
  });
});
