import type { ComponentProps } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TableFilterBar } from "@/components/table-query-controls";

function filterBar(props: Partial<ComponentProps<typeof TableFilterBar>> = {}) {
  return (
    <TableFilterBar
      filters={[]}
      onOpen={vi.fn()}
      onRemove={vi.fn()}
      onClear={vi.fn()}
      onRefresh={vi.fn()}
      {...props}
    />
  );
}

describe("TableFilterBar wrap text toggle", () => {
  it("renders no Wrap text button without onWrapTextChange", () => {
    const { rerender } = render(filterBar());
    expect(screen.getByRole("button", { name: "Refresh records" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Wrap text" })).not.toBeInTheDocument();

    rerender(filterBar({ wrapText: true }));
    expect(screen.queryByRole("button", { name: "Wrap text" })).not.toBeInTheDocument();
  });

  it("sits immediately before Refresh and reports the next state", async () => {
    const user = userEvent.setup();
    const onWrapTextChange = vi.fn();
    const { rerender } = render(filterBar({ wrapText: false, onWrapTextChange }));
    const toggle = screen.getByRole("button", { name: "Wrap text" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    expect(toggle.nextElementSibling).toBe(screen.getByRole("button", { name: "Refresh records" }));

    await user.click(toggle);
    expect(onWrapTextChange).toHaveBeenLastCalledWith(true);

    rerender(filterBar({ wrapText: true, onWrapTextChange }));
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    await user.click(toggle);
    expect(onWrapTextChange).toHaveBeenLastCalledWith(false);
  });
});
