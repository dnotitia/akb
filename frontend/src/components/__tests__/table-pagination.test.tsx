import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TablePagination } from "@/components/table-query-controls";

afterEach(cleanup);

function pagination(overrides = {}) {
  const props = { pageIndex: 2, pageSize: 50 as const, total: null, itemCount: 50,
    hasNext: true, onPageChange: vi.fn(), onPageSizeChange: vi.fn(), onCountRows: vi.fn(), ...overrides };
  render(<TablePagination {...props} />);
  return props;
}

describe("on-demand table pagination", () => {
  it("keeps later pages navigable without fabricating a corpus count", async () => {
    const props = pagination();
    expect(screen.getByText("101–150 · total not counted")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Last counted page" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(props.onPageChange).toHaveBeenCalledWith(3);
    await userEvent.click(screen.getByRole("button", { name: "Count rows" }));
    expect(props.onCountRows).toHaveBeenCalledOnce();
  });

  it("only offers Last after an exact count, keeping sentinel-based Next", async () => {
    const props = pagination({ total: 151 });
    expect(screen.getByText("101–150 · 151 rows counted")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Last counted page" }));
    expect(props.onPageChange).toHaveBeenCalledWith(3);
  });

  it("keeps empty out-of-range pages recoverable without an automatic request loop", async () => {
    const props = pagination({ pageIndex: 99, itemCount: 0, hasNext: false });
    expect(screen.getByText("0–0 · total not counted")).toBeVisible();
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "First page" }));
    expect(props.onPageChange).toHaveBeenCalledWith(0);
  });

  it("represents an exactly empty table without conflating it with unknown", () => {
    pagination({ pageIndex: 0, itemCount: 0, hasNext: false, total: 0 });
    expect(screen.getByText("0–0 · 0 rows counted")).toBeVisible();
    expect(screen.getByRole("button", { name: "Recount rows" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Last counted page" })).toBeDisabled();
  });

  it("does not cap a page jump at an older count after concurrent inserts", async () => {
    const props = pagination({ pageIndex: 0, total: 50 });
    const user = userEvent.setup();
    const input = screen.getByRole("spinbutton", { name: /Page number/ });
    await user.clear(input);
    await user.type(input, "3");
    // jsdom does not implement implicit submit for this number input.
    fireEvent.submit(input.closest("form")!);
    expect(props.onPageChange).toHaveBeenCalledWith(2);
  });
});
