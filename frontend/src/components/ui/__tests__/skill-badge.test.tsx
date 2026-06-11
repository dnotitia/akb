import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { SkillBadge } from "../skill-badge";

describe("SkillBadge", () => {
  it("renders defined state with line count", () => {
    render(<SkillBadge defined lineCount={142} />);
    expect(screen.getByText(/Guide/)).toBeTruthy();
    expect(screen.getByText(/142L/)).toBeTruthy();
  });

  it("renders undefined state with X marker", () => {
    render(<SkillBadge defined={false} />);
    const txt = screen.getByText(/Guide/).textContent || "";
    expect(txt).toContain("✗");
  });

  it("defined=true with no lineCount omits the count", () => {
    render(<SkillBadge defined />);
    expect(screen.queryByText(/\dL/)).toBeNull();
  });

  it("defined uses the teal info-outline variant (not orange); undefined is neutral outline", () => {
    const { rerender, container } = render(<SkillBadge defined />);
    const defined = container.firstChild as HTMLElement;
    // defined → teal --color-info (border-info/text-info), NEVER the
    // accent-strong ORANGE: a passive "configured" chip must not spend the
    // one-marquee-orange budget.
    expect(defined.className).toMatch(/info/);
    expect(defined.className).not.toMatch(/accent/);

    rerender(<SkillBadge defined={false} />);
    const undef = container.firstChild as HTMLElement;
    // undefined → neutral outline, no info/accent fill
    expect(undef.className).not.toMatch(/accent/);
    expect(undef.className).not.toMatch(/info/);
  });

  it("includes Sparkles icon", () => {
    const { container } = render(<SkillBadge defined />);
    const svg = container.querySelector("svg");
    expect(svg).toBeTruthy();
  });
});
