import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { SkillStatusChip } from "../skill-status-chip";

function wrap(ui: React.ReactNode) {
  return <MemoryRouter>{ui}</MemoryRouter>;
}

describe("SkillStatusChip", () => {
  it("customized → the settings guide editor", () => {
    render(wrap(<SkillStatusChip vault="my-v" defined customized />));
    expect(screen.getByText(/Guide/).textContent).toContain("customized");
    expect(screen.getByRole("link").getAttribute("href")).toBe(
      "/vault/my-v/settings#skill",
    );
  });

  it("still on the template → same destination, different state", () => {
    render(wrap(<SkillStatusChip vault="my-v" defined customized={false} />));
    expect(screen.getByText(/Guide/).textContent).toContain("template");
    expect(screen.getByRole("link").getAttribute("href")).toBe(
      "/vault/my-v/settings#skill",
    );
  });

  it("state unknown → plain marker, no guessed state", () => {
    render(wrap(<SkillStatusChip vault="my-v" defined />));
    const txt = screen.getByText(/Guide/).textContent || "";
    expect(txt).not.toContain("customized");
    expect(txt).not.toContain("template");
  });

  it("undefined → links to the same editor, marked missing", () => {
    render(wrap(<SkillStatusChip vault="my-v" defined={false} />));
    const t = screen.getByText(/Guide/);
    expect(t.textContent).toContain("✗");
    expect(screen.getByRole("link").getAttribute("href")).toBe(
      "/vault/my-v/settings#skill",
    );
  });
});
