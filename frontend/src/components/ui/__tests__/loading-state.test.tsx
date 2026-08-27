import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

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
