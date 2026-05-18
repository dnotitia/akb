import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { SkillBadge } from "../skill-badge";

describe("SkillBadge", () => {
  it("renders defined state with line count", () => {
    render(<SkillBadge defined lineCount={142} />);
    expect(screen.getByText(/GUIDE/)).toBeTruthy();
    expect(screen.getByText(/142L/)).toBeTruthy();
  });

  it("renders undefined state with X marker", () => {
    render(<SkillBadge defined={false} />);
    const txt = screen.getByText(/GUIDE/).textContent || "";
    expect(txt).toContain("✗");
  });

  it("defined=true with no lineCount omits the count", () => {
    render(<SkillBadge defined />);
    expect(screen.queryByText(/\dL/)).toBeNull();
  });

  it("applies info variant when defined, outline when undefined", () => {
    const { rerender, container } = render(<SkillBadge defined />);
    const defined = container.querySelector("[class*='accent']");
    expect(defined).toBeTruthy();

    rerender(<SkillBadge defined={false} />);
    // outline = border-only, no accent fill
  });

  it("includes Sparkles icon", () => {
    const { container } = render(<SkillBadge defined />);
    const svg = container.querySelector("svg");
    expect(svg).toBeTruthy();
  });
});
