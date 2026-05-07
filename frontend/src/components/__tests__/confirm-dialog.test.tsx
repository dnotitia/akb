import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";

function Harness({
  onConfirm,
  variant = "default" as const,
}: {
  onConfirm: () => void | Promise<void>;
  variant?: "default" | "destructive";
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)}>Open</button>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title="Revoke token?"
        description={"This cannot be undone."}
        confirmLabel="Revoke"
        variant={variant}
        onConfirm={onConfirm}
      />
    </>
  );
}

afterEach(cleanup);

describe("ConfirmDialog", () => {
  it("renders title and description when open", async () => {
    const user = userEvent.setup();
    render(<Harness onConfirm={() => {}} />);
    await user.click(screen.getByText("Open"));

    expect(screen.getByText("Revoke token?")).toBeInTheDocument();
    expect(screen.getByText("This cannot be undone.")).toBeInTheDocument();
  });

  it("calls onConfirm when confirm clicked, then closes", async () => {
    const onConfirm = vi.fn();
    const user = userEvent.setup();
    render(<Harness onConfirm={onConfirm} />);
    await user.click(screen.getByText("Open"));
    await user.click(screen.getByRole("button", { name: "Revoke" }));

    expect(onConfirm).toHaveBeenCalledOnce();
  });

  it("does not call onConfirm when cancel clicked", async () => {
    const onConfirm = vi.fn();
    const user = userEvent.setup();
    render(<Harness onConfirm={onConfirm} />);
    await user.click(screen.getByText("Open"));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("disables buttons while async confirm is pending", async () => {
    let resolve: () => void = () => {};
    const onConfirm = vi.fn(
      () => new Promise<void>((r) => { resolve = r; }),
    );
    const user = userEvent.setup();
    render(<Harness onConfirm={onConfirm} />);
    await user.click(screen.getByText("Open"));
    await user.click(screen.getByRole("button", { name: "Revoke" }));

    const cancelBtn = screen.getByRole("button", { name: "Cancel" });
    expect(cancelBtn).toBeDisabled();
    expect(screen.getByRole("button", { name: "Working…" })).toBeDisabled();

    resolve();
  });
});
