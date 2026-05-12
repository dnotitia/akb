import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { DeleteCollectionDialog } from "@/components/delete-collection-dialog";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    deleteCollection: vi.fn(),
  };
});

import { deleteCollection } from "@/lib/api";
const deleteMock = deleteCollection as unknown as ReturnType<typeof vi.fn>;

beforeEach(() => {
  deleteMock.mockReset();
});

afterEach(() => cleanup());

describe("DeleteCollectionDialog", () => {
  it("empty mode renders simple confirm and calls deleteCollection with recursive=false", async () => {
    deleteMock.mockResolvedValue({
      ok: true,
      collection: "x",
      deleted_docs: 0,
      deleted_files: 0,
    });
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    const onDeleted = vi.fn();
    render(
      <DeleteCollectionDialog
        vault="v"
        path="x"
        docCount={0}
        fileCount={0}
        open
        onOpenChange={onOpenChange}
        onDeleted={onDeleted}
      />,
    );

    // No type-to-confirm input shown in empty mode.
    expect(screen.queryByLabelText(/type the collection path/i)).not.toBeInTheDocument();

    const btn = screen.getByRole("button", { name: /^delete$/i });
    expect(btn).not.toBeDisabled();
    await user.click(btn);

    await waitFor(() =>
      expect(deleteMock).toHaveBeenCalledWith("v", "x", false),
    );
    expect(onDeleted).toHaveBeenCalled();
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("cascade mode requires the typed value to exactly match path", async () => {
    deleteMock.mockResolvedValue({
      ok: true,
      collection: "x",
      deleted_docs: 3,
      deleted_files: 1,
    });
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    const onDeleted = vi.fn();
    render(
      <DeleteCollectionDialog
        vault="v"
        path="x"
        docCount={3}
        fileCount={1}
        open
        onOpenChange={onOpenChange}
        onDeleted={onDeleted}
      />,
    );

    const btn = screen.getByRole("button", { name: /^delete$/i });
    expect(btn).toBeDisabled();

    const input = screen.getByLabelText(/type the collection path/i);
    await user.type(input, "wrong");
    expect(btn).toBeDisabled();

    await user.clear(input);
    await user.type(input, "x");
    expect(btn).not.toBeDisabled();

    await user.click(btn);
    await waitFor(() =>
      expect(deleteMock).toHaveBeenCalledWith("v", "x", true),
    );
    expect(onDeleted).toHaveBeenCalled();
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("cascade mode body text displays doc and file counts", () => {
    render(
      <DeleteCollectionDialog
        vault="v"
        path="x"
        docCount={3}
        fileCount={1}
        open
        onOpenChange={() => {}}
        onDeleted={() => {}}
      />,
    );

    // Search the dialog content for "3 document" and "1 file" substrings.
    const dialog = screen.getByRole("dialog");
    expect(dialog.textContent?.toLowerCase()).toContain("3 document");
    expect(dialog.textContent?.toLowerCase()).toContain("1 file");
  });

  it("renders inline error and keeps dialog open when the API rejects", async () => {
    deleteMock.mockRejectedValue(new Error("boom"));
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    const onDeleted = vi.fn();
    render(
      <DeleteCollectionDialog
        vault="v"
        path="x"
        docCount={0}
        fileCount={0}
        open
        onOpenChange={onOpenChange}
        onDeleted={onDeleted}
      />,
    );

    await user.click(screen.getByRole("button", { name: /^delete$/i }));

    expect(await screen.findByText(/boom/)).toBeInTheDocument();
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
    expect(onDeleted).not.toHaveBeenCalled();
  });

  it("disables the Delete button while the request is in flight", async () => {
    let resolveFn: ((v: unknown) => void) | undefined;
    deleteMock.mockReturnValue(
      new Promise((resolve) => {
        resolveFn = resolve;
      }),
    );
    const user = userEvent.setup();
    render(
      <DeleteCollectionDialog
        vault="v"
        path="x"
        docCount={0}
        fileCount={0}
        open
        onOpenChange={() => {}}
        onDeleted={() => {}}
      />,
    );

    const btn = screen.getByRole("button", { name: /^delete$/i });
    await user.click(btn);

    // While the promise is pending, the button is disabled.
    await waitFor(() => expect(btn).toBeDisabled());

    // Resolve to flush.
    resolveFn?.({
      ok: true,
      collection: "x",
      deleted_docs: 0,
      deleted_files: 0,
    });
  });
});
