import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ResourceDeleteDialog } from "@/components/resource-delete-dialog";

afterEach(() => cleanup());

describe("ResourceDeleteDialog", () => {
  it("uses a standard confirmation for documents", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn().mockResolvedValue(undefined);
    render(
      <ResourceDeleteDialog
        open
        onOpenChange={() => {}}
        kind="document"
        name="Architecture"
        onConfirm={onConfirm}
      />,
    );

    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Delete document" }));
    await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(1));
  });

  it("requires an exact table name and reports the row impact", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn().mockResolvedValue(undefined);
    render(
      <ResourceDeleteDialog
        open
        onOpenChange={() => {}}
        kind="table"
        name="audit_log"
        rowCount={23}
        onConfirm={onConfirm}
      />,
    );

    expect(screen.getByText(/all 23 rows/i)).toBeInTheDocument();
    const confirm = screen.getByRole("button", { name: "Delete table" });
    expect(confirm).toBeDisabled();

    const input = screen.getByLabelText(/type the table name/i);
    await user.type(input, "audit");
    expect(confirm).toBeDisabled();
    await user.type(input, "_log");
    expect(confirm).toBeEnabled();
    await user.click(confirm);

    await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(1));
  });

  it("keeps an API conflict inside the dialog for correction", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn().mockRejectedValue(
      new Error("Other tables still reference this table"),
    );
    render(
      <ResourceDeleteDialog
        open
        onOpenChange={() => {}}
        kind="table"
        name="parent"
        rowCount={1}
        onConfirm={onConfirm}
      />,
    );

    await user.type(screen.getByLabelText(/type the table name/i), "parent");
    await user.click(screen.getByRole("button", { name: "Delete table" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Other tables still reference this table",
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});
