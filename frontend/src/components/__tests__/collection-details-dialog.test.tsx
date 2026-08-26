import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CollectionDetailsDialog } from "@/components/collection-details-dialog";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    updateCollection: vi.fn(),
  };
});

import { ApiError, updateCollection } from "@/lib/api";

const updateMock = updateCollection as unknown as ReturnType<typeof vi.fn>;

const baseProps = {
  open: true,
  onOpenChange: vi.fn(),
  vault: "v",
  path: "architecture",
  summary: "System boundaries.",
  counts: { documents: 2, tables: 1, files: 1 },
  editable: true,
  onUpdated: vi.fn(),
};

beforeEach(() => {
  updateMock.mockReset();
  baseProps.onOpenChange.mockReset();
  baseProps.onUpdated.mockReset();
});

afterEach(() => cleanup());

describe("CollectionDetailsDialog", () => {
  it("keeps the current summary readable for read-only members", () => {
    render(<CollectionDetailsDialog {...baseProps} editable={false} />);
    expect(screen.getByText("System boundaries.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /save summary/i })).not.toBeInTheDocument();
  });

  it("surfaces a compatibility notice when the connected server has no PATCH contract", async () => {
    updateMock.mockRejectedValue(
      new ApiError("Method Not Allowed", 405, { detail: "Method Not Allowed" }),
    );
    const user = userEvent.setup();
    render(<CollectionDetailsDialog {...baseProps} />);

    const summary = screen.getByLabelText("Summary");
    await user.clear(summary);
    await user.type(summary, "Updated system boundaries.");
    await user.click(screen.getByRole("button", { name: /save summary/i }));

    expect(
      await screen.findByText(/summary editing is coming soon on this server/i),
    ).toBeInTheDocument();
    expect(baseProps.onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("refreshes and closes after a supported server saves the summary", async () => {
    updateMock.mockResolvedValue({ ok: true });
    const user = userEvent.setup();
    render(<CollectionDetailsDialog {...baseProps} />);

    const summary = screen.getByLabelText("Summary");
    await user.clear(summary);
    await user.type(summary, "Updated system boundaries.");
    await user.click(screen.getByRole("button", { name: /save summary/i }));

    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith(
        "v",
        "architecture",
        "Updated system boundaries.",
      ),
    );
    expect(baseProps.onUpdated).toHaveBeenCalledTimes(1);
    expect(baseProps.onOpenChange).toHaveBeenCalledWith(false);
  });
});
