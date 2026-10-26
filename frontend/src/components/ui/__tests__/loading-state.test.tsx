import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InlineLoadingState, LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";

describe("LoadingState", () => {
  it("exposes one named busy status and hides visual placeholders", () => {
    render(
      <LoadingState label="Loading account settings">
        <Skeleton data-testid="placeholder" className="h-8" />
      </LoadingState>,
    );

    const status = screen.getByRole("status", { name: "Loading account settings" });
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveAttribute("aria-busy", "true");
    expect(screen.getByTestId("placeholder").parentElement).toHaveAttribute("aria-hidden", "true");
  });

  it("renders a visible compact status for short transitions", () => {
    render(<InlineLoadingState label="Opening document composer…" />);

    expect(screen.getByRole("status")).toHaveTextContent("Opening document composer…");
  });
});


describe("deferred reader loading", () => {
  afterEach(() => { cleanup(); vi.useRealTimers(); });
  it("reserves geometry but skips a status flash when a fast read completes", () => {
    vi.useFakeTimers();
    const view = render(<LoadingState delayMs={200} label="Loading document"><Skeleton data-testid="reader-placeholder" className="h-8" /></LoadingState>);
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByTestId("reader-placeholder").parentElement).toHaveStyle({ visibility: "hidden" });
    act(() => vi.advanceTimersByTime(100));
    view.rerender(<p>Ready document</p>);
    act(() => vi.advanceTimersByTime(200));
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByText("Ready document")).toBeVisible();
  });
  it("reveals the skeleton for a slow read and removes it immediately on success", () => {
    vi.useFakeTimers();
    const view = render(<LoadingState delayMs={200} label="Loading document"><Skeleton data-testid="reader-placeholder" /></LoadingState>);
    act(() => vi.advanceTimersByTime(200));
    expect(screen.getByRole("status", { name: "Loading document" })).toBeVisible();
    expect(screen.getByTestId("reader-placeholder").parentElement).not.toHaveStyle({ visibility: "hidden" });
    view.rerender(<p>Ready document</p>);
    expect(screen.queryByRole("status")).toBeNull();
  });
});
