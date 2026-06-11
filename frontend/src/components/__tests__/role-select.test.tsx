import { render, screen, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { RoleSelect } from "../role-select";
import * as api from "@/lib/api";

vi.mock("@/lib/api", () => ({ grantAccess: vi.fn() }));

afterEach(cleanup);

const baseMember = {
  username: "alice",
  display_name: "Alice",
  email: "alice@t.dev",
  role: "reader" as const,
  since: null,
};

// The role control is now a themed dropdown (Radix DropdownMenu) rather than a
// native <select>, so its options live in a popover opened from the badge.
async function openRoleMenu() {
  const user = userEvent.setup();
  await user.click(screen.getByLabelText(/change role for alice/i));
  return user;
}

describe("RoleSelect", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders three role options with the current one checked", async () => {
    render(<RoleSelect vault="v" member={baseMember} onChanged={() => {}} />);
    await openRoleMenu();
    const items = await screen.findAllByRole("menuitemradio");
    expect(items.map((i) => i.textContent?.trim())).toEqual([
      "reader", "writer", "admin",
    ]);
    expect(items[0]).toHaveAttribute("aria-checked", "true");
  });

  it("calls grantAccess on change and reports prev/next", async () => {
    (api.grantAccess as any).mockResolvedValue({});
    const onChanged = vi.fn();
    render(<RoleSelect vault="v" member={baseMember} onChanged={onChanged} />);
    const user = await openRoleMenu();
    await user.click(await screen.findByRole("menuitemradio", { name: /writer/i }));
    await waitFor(() => expect(api.grantAccess).toHaveBeenCalledWith("v", "alice", "writer"));
    expect(onChanged).toHaveBeenCalledWith("reader", "writer");
  });

  it("surfaces inline error on rejection", async () => {
    (api.grantAccess as any).mockRejectedValue(new Error("boom"));
    render(<RoleSelect vault="v" member={baseMember} onChanged={() => {}} />);
    const user = await openRoleMenu();
    await user.click(await screen.findByRole("menuitemradio", { name: /writer/i }));
    expect(await screen.findByText(/boom/i)).toBeInTheDocument();
  });

  it("ignores same-value change", async () => {
    render(<RoleSelect vault="v" member={baseMember} onChanged={() => {}} />);
    const user = await openRoleMenu();
    await user.click(await screen.findByRole("menuitemradio", { name: /reader/i }));
    expect(api.grantAccess).not.toHaveBeenCalled();
  });
});
