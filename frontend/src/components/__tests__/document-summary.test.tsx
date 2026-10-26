import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DocumentSummary } from "../document-summary";

describe("DocumentSummary", () => {
  let onResize: ResizeObserverCallback;
  let onIntersection: IntersectionObserverCallback;
  const disconnectResize = vi.fn();
  const disconnectIntersection = vi.fn();
  beforeEach(() => {
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(240);
    vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockImplementation(function (this: HTMLElement) {
      return (this.textContent?.length ?? 0) > 40 ? 1000 : 120;
    });
    vi.stubGlobal("ResizeObserver", class { constructor(callback: ResizeObserverCallback) { onResize = callback; } observe() {} disconnect = disconnectResize; });
    vi.stubGlobal("IntersectionObserver", class {
      constructor(callback: IntersectionObserverCallback) { onIntersection = callback; }
      observe() {} disconnect = disconnectIntersection;
    });
  });
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it("shows a short summary as ordinary text without motion controls or a disclosure", () => {
    const { rerender } = render(<DocumentSummary summary="A short summary." />);
    const region = screen.getByRole("region", { name: "Document summary" });
    expect(region).toHaveTextContent("A short summary.");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(region).not.toHaveAttribute("tabindex");
    expect(region.parentElement).toHaveAttribute("data-overflow", "false");
    rerender(<DocumentSummary summary="  " />);
    expect(screen.queryByRole("region")).not.toBeInTheDocument();
  });

  it("keeps the complete long summary accessible and supports persistent keyboard pause/resume", async () => {
    const user = userEvent.setup();
    const summary = "A long summary that remains readable in full. ".repeat(20);
    render(<DocumentSummary summary={summary} />);
    const region = screen.getByRole("region", { name: "Document summary" });
    expect(region.textContent).toBe(summary);
    expect(region.parentElement).toHaveAttribute("data-overflow", "true");
    expect(region.parentElement?.style.getPropertyValue("--summary-distance")).toBe("-760px");
    await user.tab();
    expect(region).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Pause summary scrolling" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(region.parentElement).toHaveAttribute("data-paused", "true");
    await user.keyboard(" ");
    expect(screen.getByRole("button", { name: "Pause summary scrolling" })).toHaveFocus();
    expect(region.parentElement).toHaveAttribute("data-paused", "false");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("moves the final characters fully beyond the faded edge", () => {
    render(<DocumentSummary summary={"A long summary. ".repeat(20)} />);
    const region = screen.getByRole("region", { name: "Document summary" });
    region.style.setProperty("--summary-edge-clearance", "28px");
    act(() => onResize([], {} as ResizeObserver));
    expect(region.parentElement?.style.getPropertyValue("--summary-distance")).toBe("-788px");
  });

  it("pauses offscreen text, resets a new summary and disconnects observers on unmount", async () => {
    const user = userEvent.setup();
    const { rerender, unmount } = render(<DocumentSummary summary={"Original summary. ".repeat(20)} />);
    const region = screen.getByRole("region", { name: "Document summary" });
    act(() => onIntersection([{ isIntersecting: false } as IntersectionObserverEntry], {} as IntersectionObserver));
    expect(region.parentElement).toHaveAttribute("data-paused", "true");
    act(() => onIntersection([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver));
    expect(region.parentElement).toHaveAttribute("data-paused", "false");
    await user.click(screen.getByRole("button", { name: "Pause summary scrolling" }));
    rerender(<DocumentSummary summary={"A different document summary. ".repeat(10)} />);
    expect(screen.queryByText(/Original summary/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pause summary scrolling" })).toBeInTheDocument();
    unmount();
    expect(disconnectResize).toHaveBeenCalled();
    expect(disconnectIntersection).toHaveBeenCalled();
  });
});
