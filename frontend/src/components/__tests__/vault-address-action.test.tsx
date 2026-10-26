import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { VaultAddressAction } from "../vault-address-action";

describe("VaultAddressAction", () => {
  it("copies the address from one icon and confirms completion", async () => {
    const user = userEvent.setup();
    const write = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    render(<VaultAddressAction name="research" />);
    expect(screen.queryByText("akb://research")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Copy akb://research" }));
    expect(write).toHaveBeenCalledWith("akb://research");
    expect(screen.getByRole("status")).toHaveTextContent("Vault address copied");
  });

  it("offers a selectable address when clipboard access fails", async () => {
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("Blocked"));
    render(<VaultAddressAction name="research" />);
    await user.click(screen.getByRole("button", { name: "Copy akb://research" }));
    expect(screen.getByRole("dialog", { name: "Copy vault address" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Vault address" })).toHaveValue("akb://research");
    expect(screen.getByRole("status", { hidden: true })).toBeEmptyDOMElement();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Copy akb://research" })).toHaveFocus();
  });
});
