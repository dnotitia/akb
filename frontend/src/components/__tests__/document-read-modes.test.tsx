import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { DocumentReadModes } from "../document-reading-controls";

function Modes() {
  const [view, setView] = useState<"rendered" | "raw">("rendered");
  return <><DocumentReadModes view={view} onChange={setView} idPrefix="reader" />
    <div id={`reader-panel-${view}`} role="tabpanel" aria-labelledby={`reader-tab-${view}`}>{view}</div></>;
}

describe("DocumentReadModes", () => {
  it("is a segmented view control, distinct from page-navigation underlines", async () => {
    const user = userEvent.setup();
    render(<Modes />);
    const rendered = screen.getByRole("tab", { name: "Rendered" });
    const raw = screen.getByRole("tab", { name: "Raw" });
    expect(rendered).toHaveClass("bg-surface-selected");
    expect(rendered.className).not.toContain("after:");
    await user.tab();
    expect(rendered).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    expect(raw).toHaveFocus();
    expect(raw).toHaveAttribute("aria-selected", "false");
    await user.keyboard("{Enter}");
    expect(raw).toHaveAttribute("aria-selected", "true");
    expect(raw).toHaveAttribute("tabIndex", "0");
    expect(screen.getByRole("tabpanel", { name: "Raw" })).toBeInTheDocument();
    await user.keyboard("{Home}{Enter}");
    expect(rendered).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{End} ");
    expect(raw).toHaveAttribute("aria-selected", "true");
  });
});
