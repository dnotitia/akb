import { useRef, useState } from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { VaultCreateDialog } from "@/components/vault-create-dialog";

vi.mock("@/lib/api", () => ({
  listVaultTemplates: vi.fn(),
  createVault: vi.fn(),
}));

import { createVault, listVaultTemplates } from "@/lib/api";

function DialogHarness() {
  const [open, setOpen] = useState(false);
  const [created, setCreated] = useState("");
  const triggerRef = useRef<HTMLElement | null>(null);
  return (
    <MemoryRouter>
      <button
        type="button"
        onClick={(event) => {
          triggerRef.current = event.currentTarget;
          setOpen(true);
        }}
      >
        New vault
      </button>
      <VaultCreateDialog
        open={open}
        onOpenChange={setOpen}
        onCreated={(name) => {
          setCreated(name);
          setOpen(false);
        }}
        returnFocusRef={triggerRef}
      />
      {created && <output>{created}</output>}
    </MemoryRouter>
  );
}

describe("VaultCreateDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listVaultTemplates).mockResolvedValue([]);
    vi.mocked(createVault).mockResolvedValue({ vault_id: "v1", name: "engineering" } as any);
  });

  afterEach(cleanup);

  it("opens with focus in the name field and returns focus when cancelled", async () => {
    const user = userEvent.setup();
    render(<DialogHarness />);

    const trigger = screen.getByRole("button", { name: "New vault" });
    await user.click(trigger);

    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(screen.getByLabelText(/^name/i)).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
  });

  it("creates the vault and closes after a successful submit", async () => {
    const user = userEvent.setup();
    render(<DialogHarness />);

    await user.click(screen.getByRole("button", { name: "New vault" }));
    await user.type(await screen.findByLabelText(/^name/i), "engineering");
    await user.click(screen.getByRole("button", { name: "Create vault" }));

    await waitFor(() =>
      expect(createVault).toHaveBeenCalledWith("engineering", undefined, undefined),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByText("engineering")).toBeInTheDocument();
  });
});
