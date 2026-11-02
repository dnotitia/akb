import { describe, expect, it } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { DocumentOutline } from "@/components/doc-outline";

function Harness({ md }: { md: string }) {
  const [el, setEl] = useState<HTMLElement | null>(null);
  return (
    <div>
      <article ref={setEl}>
        <h1 id="intro">Intro</h1>
        <h2 id="details">Details</h2>
        <h3 id="기술-스택">기술 스택</h3>
      </article>
      <DocumentOutline markdown={md} articleEl={el} />
    </div>
  );
}

const md = "# Intro\n\n## Details\n\n### 기술 스택\n";

describe("DocumentOutline", () => {
  it("renders one link per parsed heading", () => {
    render(<Harness md={md} />);
    expect(screen.getByRole("link", { name: "Intro" })).toHaveAttribute("href", "#intro");
    expect(screen.getByRole("link", { name: "Details" })).toHaveAttribute("href", "#details");
    expect(screen.getByRole("link", { name: "기술 스택" })).toHaveAttribute("href", "#기술-스택");
    cleanup();
  });

  it("preserves heading order (same as source)", () => {
    render(<Harness md={md} />);
    const items = screen.getAllByRole("listitem");
    expect(items.map((li) => li.textContent)).toEqual(["Intro", "Details", "기술 스택"]);
    cleanup();
  });

  it("renders nothing when no headings are present", () => {
    render(<Harness md={"just a paragraph\nwith no headings"} />);
    expect(screen.queryByText(/§ OUTLINE/)).not.toBeInTheDocument();
    cleanup();
  });

  it("clicking an outline link updates the URL hash", async () => {
    const user = userEvent.setup();
    render(<Harness md={md} />);
    await user.click(screen.getByRole("link", { name: "Details" }));
    expect(location.hash).toBe("#details");
    cleanup();
  });
  it("keeps ancestors in search and restores the collapsed hierarchy afterwards", async () => {
    const user = userEvent.setup(); render(<Harness md={md} />);
    await user.click(screen.getByRole("button", { name: "Collapse Details" }));
    expect(screen.queryByRole("link", { name: "기술 스택" })).toBeNull();
    await user.type(screen.getByRole("searchbox", { name: "Find a section" }), "기술");
    expect(screen.getByRole("link", { name: "Intro" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Details" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "기술 스택" })).toBeInTheDocument();
    await user.clear(screen.getByRole("searchbox", { name: "Find a section" }));
    expect(screen.queryByRole("link", { name: "기술 스택" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Expand all" }));
    expect(screen.getByRole("link", { name: "기술 스택" })).toBeInTheDocument();
    cleanup();
  });

});
