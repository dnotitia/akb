import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { InviteMemberDialog } from "@/components/invite-member-dialog";

vi.mock("@/lib/api", () => ({
  searchUsers: vi.fn(),
  grantAccess: vi.fn(),
}));

vi.mock("@/hooks/use-debounce", () => ({
  useDebounce: <T,>(value: T) => value,
}));

import { grantAccess, searchUsers } from "@/lib/api";

const searchUsersMock = searchUsers as unknown as ReturnType<typeof vi.fn>;
const grantAccessMock = grantAccess as unknown as ReturnType<typeof vi.fn>;

const USERS = [
  { username: "mina", display_name: "Mina Park", email: "mina@example.com" },
  { username: "dana", display_name: "Dana Lee", email: "dana@example.com" },
  {
    username: "already-here",
    display_name: "Already Member",
    email: "member@example.com",
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  searchUsersMock.mockResolvedValue({ users: USERS });
  grantAccessMock.mockResolvedValue(undefined);
});

afterEach(cleanup);

describe("InviteMemberDialog redesign", () => {
  it("connects account selection, role review, and the final invite action", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    const onInvited = vi.fn();

    render(
      <InviteMemberDialog
        open
        onOpenChange={onOpenChange}
        vault="platform-docs"
        existingUsernames={new Set(["already-here"])}
        onInvited={onInvited}
      />,
    );

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Invite member" })).toBeInTheDocument();
    expect(within(dialog).getByText("platform-docs")).toBeInTheDocument();

    const mina = await within(dialog).findByRole("option", { name: /Mina Park/i });
    expect(within(dialog).queryByRole("option", { name: /Already Member/i })).toBeNull();
    expect(within(dialog).getByRole("radio", { name: /Reader/i })).toBeChecked();
    expect(within(dialog).getByRole("button", { name: "Invite as Reader" })).toBeDisabled();

    await user.click(mina);
    expect(mina).toHaveAttribute("aria-selected", "true");
    expect(within(dialog).getByTestId("invite-summary")).toHaveTextContent(
      "Mina Park will join as Reader.",
    );

    await user.click(within(dialog).getByRole("radio", { name: /Writer/i }));
    expect(within(dialog).getByTestId("invite-summary")).toHaveTextContent(
      "Mina Park will join as Writer.",
    );

    await user.click(within(dialog).getByRole("button", { name: "Invite as Writer" }));
    await waitFor(() => {
      expect(grantAccessMock).toHaveBeenCalledWith("platform-docs", "mina", "writer");
    });
    expect(onInvited).toHaveBeenCalledTimes(1);
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("keeps the dialog open while an invite is being granted", async () => {
    const user = userEvent.setup();
    let resolveGrant: (() => void) | undefined;
    grantAccessMock.mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          resolveGrant = resolve;
        }),
    );
    const onOpenChange = vi.fn();

    render(
      <InviteMemberDialog
        open
        onOpenChange={onOpenChange}
        vault="platform-docs"
        existingUsernames={new Set()}
        onInvited={vi.fn()}
      />,
    );

    const dialog = screen.getByRole("dialog");
    await user.click(await within(dialog).findByRole("option", { name: /Dana Lee/i }));
    await user.click(within(dialog).getByRole("button", { name: "Invite as Reader" }));
    expect(within(dialog).getByRole("button", { name: "Inviting…" })).toBeDisabled();

    await user.click(within(dialog).getByRole("button", { name: "Close dialog" }));
    expect(onOpenChange).not.toHaveBeenCalled();
    resolveGrant?.();
  });
});
