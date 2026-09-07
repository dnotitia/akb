import { useRef, useState } from "react";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { VaultCreateDialog } from "@/components/vault-create-dialog";

vi.mock("@/lib/api", () => ({
  listVaultTemplates: vi.fn(),
  listVaults: vi.fn(),
  createVault: vi.fn(),
}));

import { createVault, listVaults, listVaultTemplates } from "@/lib/api";

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
        onOpenExisting={(name) => {
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
    vi.mocked(listVaults).mockResolvedValue({ vaults: [] });
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

  it("explains installation-wide uniqueness and enforces the backend name grammar", async () => {
    const user = userEvent.setup();
    render(<DialogHarness />);

    await user.click(screen.getByRole("button", { name: "New vault" }));
    expect(screen.getByText(/unique across this AKB installation/i)).toBeInTheDocument();

    const name = await screen.findByLabelText(/^name/i);
    await user.type(name, "two--words");
    expect(screen.getByRole("button", { name: "Create vault" })).toBeDisabled();
  });

  it("keeps a hidden-name conflict generic and offers no inaccessible target", async () => {
    const user = userEvent.setup();
    vi.mocked(createVault).mockRejectedValue({
      message: "legacy text that must not be trusted",
      detail: { code: "vault_name_unavailable" },
    });
    vi.mocked(listVaults).mockResolvedValue({ vaults: [{ name: "visible-vault" }] });
    render(<DialogHarness />);

    await user.click(screen.getByRole("button", { name: "New vault" }));
    await user.type(await screen.findByLabelText(/^name/i), "hidden-vault");
    await user.click(screen.getByRole("button", { name: "Create vault" }));

    expect(
      await screen.findByText("Vault name is unavailable. Choose a different name."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open existing vault" })).not.toBeInTheDocument();
    expect(screen.queryByText(/legacy text/i)).not.toBeInTheDocument();
  });

  it("offers recovery only when the conflicting vault is already accessible", async () => {
    const user = userEvent.setup();
    vi.mocked(createVault).mockRejectedValue({
      detail: { code: "vault_name_unavailable" },
    });
    vi.mocked(listVaults).mockResolvedValue({ vaults: [{ name: "engineering" }] });
    render(<DialogHarness />);

    await user.click(screen.getByRole("button", { name: "New vault" }));
    await user.type(await screen.findByLabelText(/^name/i), "engineering");
    await user.click(screen.getByRole("button", { name: "Create vault" }));
    expect(
      await screen.findByRole("status", { name: "" }),
    ).toHaveTextContent("An accessible vault with this name can be opened.");
    await user.click(await screen.findByRole("button", { name: "Open existing vault" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByText("engineering")).toBeInTheDocument();
  });

  it("unlocks immediately after conflict and ignores a stale accessibility lookup", async () => {
    const user = userEvent.setup();
    let resolveVaults: ((value: { vaults: Array<{ name: string }> }) => void) | undefined;
    vi.mocked(createVault).mockRejectedValue({
      detail: { code: "vault_name_unavailable" },
    });
    vi.mocked(listVaults).mockReturnValue(
      new Promise((resolve) => {
        resolveVaults = resolve;
      }),
    );
    render(<DialogHarness />);

    await user.click(screen.getByRole("button", { name: "New vault" }));
    const name = await screen.findByLabelText(/^name/i);
    await user.type(name, "engineering");
    await user.click(screen.getByRole("button", { name: "Create vault" }));

    expect(
      await screen.findByText("Vault name is unavailable. Choose a different name."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create vault" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled();

    await user.clear(name);
    await user.type(name, "another-vault");
    await act(async () => {
      resolveVaults?.({ vaults: [{ name: "engineering" }] });
      await Promise.resolve();
    });

    expect(screen.queryByRole("button", { name: "Open existing vault" })).not.toBeInTheDocument();
  });
});
