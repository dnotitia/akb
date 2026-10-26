import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  act,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import VaultMembersPage from "@/pages/vault-members";
import { CurrentUserProvider } from "@/contexts/current-user-context";

vi.mock("@/lib/api", () => ({
  getMe: vi.fn(),
  getVaultInfo: vi.fn(),
  getVaultMembers: vi.fn(),
  grantAccess: vi.fn(),
  revokeAccess: vi.fn(),
  searchUsers: vi.fn().mockResolvedValue({ users: [] }),
  transferOwnership: vi.fn(),
}));

import {
  getMe,
  getVaultInfo,
  getVaultMembers,
  grantAccess,
  revokeAccess,
  transferOwnership,
} from "@/lib/api";

const getMeMock = getMe as unknown as ReturnType<typeof vi.fn>;
const getVaultInfoMock = getVaultInfo as unknown as ReturnType<typeof vi.fn>;
const getVaultMembersMock = getVaultMembers as unknown as ReturnType<
  typeof vi.fn
>;

const MEMBERS = [
  {
    id: "user-owner",
    username: "kwoo",
    display_name: "Vault Owner",
    email: "owner@example.com",
    role: "owner",
    since: "2026-03-14T00:00:00Z",
    account_kind: "human",
    account_status: "active",
  },
  {
    id: "user-mina",
    username: "mina",
    display_name: "Mina Park",
    email: "mina@example.com",
    role: "writer",
    since: "2026-04-02T00:00:00Z",
    account_kind: "human",
    account_status: "active",
  },
  {
    id: "user-dana",
    username: "dana",
    display_name: "Dana Lee",
    email: "dana@example.com",
    role: "reader",
    since: "2026-06-20T00:00:00Z",
    account_kind: "human",
    account_status: "active",
  },
] as const;

function pageElement({ account = "account-a", checking = false, revision = 0 } = {}) {
  return (
    <CurrentUserProvider user={{ user_id: account } as never} checking={checking} revision={revision}>
    <MemoryRouter initialEntries={["/vault/platform-docs/members"]}>
      <Link to="/vault/other-vault/members">Switch Vault</Link>
      <Routes>
        <Route path="/vault/:name/members" element={<VaultMembersPage />} />
      </Routes>
    </MemoryRouter>
    </CurrentUserProvider>
  );
}

function renderPage() { return render(pageElement()); }

beforeEach(() => {
  vi.clearAllMocks();
  getMeMock.mockResolvedValue({ user_id: "user-owner", username: "kwoo" });
  getVaultInfoMock.mockResolvedValue({
    name: "platform-docs",
    role: "owner",
    role_source: "member",
    public_access: "none",
  });
  getVaultMembersMock.mockResolvedValue({ members: MEMBERS });
});

afterEach(cleanup);

describe("Vault Members redesign", () => {
  it("prioritizes one roster and discloses role guidance only on request", async () => {
    const user = userEvent.setup();
    renderPage();
    const roster = await screen.findByRole("table", { name: "Vault members" });
    expect(
      screen.getByRole("heading", { level: 1, name: "Members" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 2, name: "Members" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("complementary", { name: "Member access context" }),
    ).toBeNull();
    expect(
      screen.queryByRole("list", {
        name: "Roles from highest to lowest access",
      }),
    ).toBeNull();
    expect(screen.getByText("3 members")).toBeInTheDocument();
    expect(within(roster).getByText("Vault Owner")).toBeInTheDocument();
    expect(within(roster).getByText("You")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Settings/ })).toBeNull();
    expect(screen.queryByText(/People not listed here/)).toBeNull();
    expect(screen.getByRole("button", { name: "Invite member" })).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "Change role for mina" }),
    ).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "More actions for mina" }),
    ).toBeEnabled();
    const trigger = within(roster).getByRole("button", {
      name: "Role permissions",
    });
    await user.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "Role permissions" });
    const ladder = within(dialog).getByRole("list", {
      name: "Roles from highest to lowest access",
    });
    expect(
      within(ladder)
        .getAllByRole("listitem")
        .map(
          (item) =>
            within(item).getByText(/^(Owner|Admin|Writer|Reader)$/).textContent,
        ),
    ).toEqual(["Owner", "Admin", "Writer", "Reader"]);
    expect(within(ladder).getAllByRole("listitem")[0]).toHaveAttribute(
      "aria-current",
      "true",
    );
    await user.keyboard("{Escape}");
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("keeps unknown policy unknown rather than reporting Private", async () => {
    getVaultInfoMock.mockRejectedValue(new Error("Unavailable"));
    renderPage();
    expect(
      await screen.findByRole("table", { name: "Vault members" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/People not listed here/)).toBeNull();
    expect(screen.queryByText("Private", { exact: true })).toBeNull();
    expect(screen.queryByRole("button", { name: "Invite member" })).toBeNull();
  });

  it("retains filtering and its recovery without hiding the active filter", async () => {
    getVaultMembersMock.mockResolvedValue({
      members: [
        ...MEMBERS,
        ...Array.from({ length: 8 }, (_, i) => ({
          id: `user-${i}`,
          username: `user-${i}`,
          email: `user${i}@example.com`,
          role: "reader",
          account_kind: "human",
          account_status: "active",
        })),
      ],
    });
    const user = userEvent.setup();
    renderPage();
    const search = await screen.findByRole("searchbox", {
      name: "Filter members",
    });
    await user.type(search, "MINA@");
    expect(
      screen.getByRole("table", { name: "Vault members" }),
    ).toHaveTextContent("Mina Park");
    expect(
      screen.getByRole("table", { name: "Vault members" }),
    ).not.toHaveTextContent("Dana Lee");
    await user.clear(search);
    await user.type(search, "missing");
    await user.click(screen.getByRole("button", { name: "Clear filter" }));
    expect(search).toHaveValue("");
    expect(
      screen.getByRole("table", { name: "Vault members" }),
    ).toHaveTextContent("Dana Lee");
  });

  it.each(["is_archived", "is_external_git"])(
    "does not promise public writes when %s restricts content",
    async (restriction) => {
      getVaultInfoMock.mockResolvedValue({
        name: "platform-docs",
        role: "owner",
        public_access: "writer",
        [restriction]: true,
      });
      renderPage();
      await screen.findByRole("table", { name: "Vault members" });
      expect(
        screen.getByText(
          "People not listed here can also read this vault when signed in. Content is read-only.",
        ),
      ).toBeInTheDocument();
    },
  );

  it("opens the member action menu and keeps destructive actions explicit", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(
      await screen.findByRole("button", { name: "More actions for mina" }),
    );
    expect(
      screen.getByRole("menuitem", { name: "Transfer ownership" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("menuitem", { name: "Revoke access" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("menuitem", { name: "Revoke access" }));
    expect(await screen.findByText("Revoke Mina Park (@mina)?")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Revoke access" }),
    ).toBeInTheDocument();
  });

  it("renders a genuinely read-only roster for readers", async () => {
    getVaultInfoMock.mockResolvedValue({
      name: "platform-docs",
      role: "reader",
      role_source: "member",
      public_access: "none",
    });
    getMeMock.mockResolvedValue({ user_id: "user-dana", username: "dana" });
    renderPage();

    await waitFor(() =>
      expect(screen.getByText(/roster is read-only/i)).toBeInTheDocument(),
    );
    expect(screen.queryByRole("button", { name: "Invite member" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: /Change role for/i }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: /More actions for/i }),
    ).toBeNull();
    expect(
      screen.queryByRole("link", { name: /Change in Settings/i }),
    ).toBeNull();
    expect(screen.queryByText(/People not listed here/)).toBeNull();
  });

  it("keeps role changes and Undo connected to the API", async () => {
    vi.mocked(grantAccess).mockResolvedValue({} as never);
    const user = userEvent.setup();
    renderPage();
    await user.click(
      await screen.findByRole("button", { name: "Change role for mina" }),
    );
    await user.click(screen.getByRole("menuitemradio", { name: "Admin" }));
    expect(grantAccess).toHaveBeenCalledWith(
      "platform-docs",
      { id: "user-mina", username: "mina" },
      "admin",
    );
    await user.click(await screen.findByRole("button", { name: "Undo" }));
    expect(grantAccess).toHaveBeenLastCalledWith(
      "platform-docs",
      { id: "user-mina", username: "mina" },
      "writer",
    );
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Undo" })).toBeNull(),
    );
  });

  it.each(["revoke", "transfer"] as const)(
    "requires confirmation before %s and refreshes afterward",
    async (action) => {
      const user = userEvent.setup();
      vi.mocked(revokeAccess).mockResolvedValue({} as never);
      vi.mocked(transferOwnership).mockResolvedValue({} as never);
      renderPage();
      await user.click(
        await screen.findByRole("button", { name: "More actions for mina" }),
      );
      const label =
        action === "revoke" ? "Revoke access" : "Transfer ownership";
      const api = action === "revoke" ? revokeAccess : transferOwnership;
      await user.click(screen.getByRole("menuitem", { name: label }));
      expect(api).not.toHaveBeenCalled();
      await user.click(
        within(screen.getByRole("dialog")).getByRole("button", { name: label }),
      );
      expect(api).toHaveBeenCalledWith("platform-docs", {
        id: "user-mina",
        username: "mina",
      });
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      expect(getVaultMembers).toHaveBeenCalledTimes(2);
    },
  );

  it("waits for the current identity before exposing per-member management", async () => {
    getMeMock.mockRejectedValue(new Error("Session unavailable"));
    renderPage();
    await screen.findByRole("table", { name: "Vault members" });
    expect(
      screen.queryByRole("button", { name: /Change role for/ }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: /More actions for/ }),
    ).toBeNull();
  });

  it("keeps member management for admins without implying owner-only policy control", async () => {
    getVaultInfoMock.mockResolvedValue({
      name: "platform-docs",
      role: "admin",
      role_source: "member",
      public_access: "reader",
    });
    getMeMock.mockResolvedValue({ user_id: "user-admin", username: "admin-user" });
    renderPage();

    expect(
      await screen.findByRole("button", { name: "Invite member" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /Change in Settings/i }),
    ).toBeNull();
    expect(
      screen.getByText(
        "People not listed here can also read this vault when signed in.",
      ),
    ).toBeInTheDocument();
  });

  it.each([
    [
      "reader",
      "People not listed here can also read this vault when signed in.",
    ],
    [
      "writer",
      "People not listed here can also read and change content when signed in.",
    ],
  ])(
    "explains effective %s access without turning the roster into public settings",
    async (publicAccess, note) => {
      getVaultInfoMock.mockResolvedValue({
        name: "platform-docs",
        role: "owner",
        public_access: publicAccess,
      });
      renderPage();
      const roster = await screen.findByRole("table", {
        name: "Vault members",
      });
      const notice = screen.getByText(note);
      expect(notice).toBeVisible();
      expect(
        roster.compareDocumentPosition(notice) &
          Node.DOCUMENT_POSITION_FOLLOWING,
      ).not.toBe(0);
      expect(screen.queryByRole("link", { name: /Settings/ })).toBeNull();
      expect(
        screen.getByRole("button", { name: "Change role for mina" }),
      ).toBeEnabled();
    },
  );

  it.each([
    ["none", "This Vault does not grant public access to signed-in non-members."],
    ["reader", "They can still read this Vault when signed in under its public access policy."],
    ["writer", "They can still read and change content when signed in under this Vault's public access policy."],
    [undefined, "The public access policy is unknown, so remaining access cannot be confirmed."],
  ])("explains what revoking membership leaves for public policy %s", async (policy, remaining) => {
    getVaultInfoMock.mockResolvedValue({ role: "owner", public_access: policy });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "More actions for mina" }));
    await user.click(screen.getByRole("menuitem", { name: "Revoke access" }));
    const dialog = screen.getByRole("dialog", { name: "Revoke Mina Park (@mina)?" });
    expect(dialog).toHaveTextContent("Mina Park (@mina)'s member permissions on platform-docs will be removed.");
    expect(dialog).toHaveTextContent(remaining);
    expect(dialog).not.toHaveTextContent("will lose access");
  });

  it("does not promise public writes on a mirror when revoking membership", async () => {
    getVaultInfoMock.mockResolvedValue({ role: "owner", public_access: "writer", is_external_git: true });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "More actions for mina" }));
    await user.click(screen.getByRole("menuitem", { name: "Revoke access" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("Content is read-only in this Vault.");
  });

  it("disables archived membership mutations and preserves ownership transfer", async () => {
    getVaultInfoMock.mockResolvedValue({ role: "owner", public_access: "reader", is_archived: true });
    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByRole("button", { name: "Invite member" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Change role for mina" })).toBeDisabled();
    expect(screen.getByText(/Vault 'platform-docs' is archived \(read-only\)/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "More actions for mina" }));
    expect(screen.getByRole("menuitem", { name: "Revoke access" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("menuitem", { name: "Transfer ownership" })).not.toHaveAttribute("aria-disabled", "true");
    await user.click(screen.getByRole("menuitem", { name: "Transfer ownership" }));
    expect(screen.getByRole("dialog", { name: "Transfer ownership to Mina Park (@mina)?" })).toBeInTheDocument();
    expect(grantAccess).not.toHaveBeenCalled();
    expect(revokeAccess).not.toHaveBeenCalled();
  });

  it.each(["Vault", "account", "revision"])("ignores a late roster after the %s boundary changes", async (boundary) => {
    let resolveOld!: (value: unknown) => void;
    getVaultMembersMock.mockReturnValueOnce(new Promise((resolve) => { resolveOld = resolve; }));
    const nextMembers = [{ username: "new-member", display_name: "New roster", email: "new@example.com", role: "reader" }];
    getVaultMembersMock.mockResolvedValue({ members: nextMembers });
    const user = userEvent.setup();
    const page = renderPage();
    await waitFor(() => expect(getVaultMembers).toHaveBeenCalledTimes(1));
    if (boundary === "Vault") await user.click(screen.getByRole("link", { name: "Switch Vault" }));
    else page.rerender(pageElement(boundary === "account" ? { account: "account-b" } : { revision: 1 }));
    await screen.findByText("New roster");
    await act(async () => { resolveOld({ members: MEMBERS }); });
    expect(screen.getByRole("table", { name: "Vault members" })).toHaveTextContent("New roster");
    expect(screen.queryByText("Mina Park")).toBeNull();
  });

  it("hides private data and clears confirmations during access verification", async () => {
    const user = userEvent.setup();
    const page = renderPage();
    await user.click(await screen.findByRole("button", { name: "More actions for mina" }));
    await user.click(screen.getByRole("menuitem", { name: "Revoke access" }));
    page.rerender(pageElement({ checking: true }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByText("Mina Park")).toBeNull();
    expect(screen.queryByRole("button", { name: "Invite member" })).toBeNull();
    expect(getVaultMembers).toHaveBeenCalledTimes(1);
    getVaultInfoMock.mockResolvedValue({ role: "reader", public_access: "none" });
    page.rerender(pageElement({ revision: 1 }));
    await screen.findByRole("table", { name: "Vault members" });
    expect(screen.queryByRole("button", { name: "Invite member" })).toBeNull();
    expect(getVaultMembers).toHaveBeenCalledTimes(2);
  });

  it("does not create Undo or reload the old Vault after a late role change", async () => {
    let resolveChange!: (value: unknown) => void;
    vi.mocked(grantAccess).mockReturnValueOnce(new Promise((resolve) => { resolveChange = resolve; }) as never);
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Change role for mina" }));
    await user.click(screen.getByRole("menuitemradio", { name: "Admin" }));
    await user.click(screen.getByRole("link", { name: "Switch Vault" }));
    await waitFor(() => expect(getVaultMembers).toHaveBeenCalledTimes(2));
    await act(async () => { resolveChange({}); });
    expect(screen.queryByRole("button", { name: "Undo" })).toBeNull();
    expect(getVaultMembers).toHaveBeenCalledTimes(2);
  });

  it.each(["admin", "owner"])("blocks managed Vault membership writes for a browser %s", async (role) => {
    getVaultInfoMock.mockResolvedValue({ role, role_source: "member", managed_by: "collector", public_access: "writer" });
    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByRole("button", { name: "Invite member" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Change role for mina" })).toBeDisabled();
    expect(screen.getByText("Vault 'platform-docs' is write-managed by collector; writes require a granted service token with the required write action")).toBeInTheDocument();
    expect(screen.queryByText(/Restore the Vault/)).toBeNull();
    await user.click(screen.getByRole("button", { name: "More actions for mina" }));
    const revoke = screen.getByRole("menuitem", { name: "Revoke access" });
    expect(revoke).toHaveAttribute("aria-disabled", "true");
    await user.click(revoke);
    if (role === "owner") {
      const transfer = screen.getByRole("menuitem", { name: "Transfer ownership" });
      expect(transfer).toHaveAttribute("aria-disabled", "true");
      await user.click(transfer);
    } else {
      expect(screen.queryByRole("menuitem", { name: "Transfer ownership" })).toBeNull();
    }
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(grantAccess).not.toHaveBeenCalled();
    expect(revokeAccess).not.toHaveBeenCalled();
    expect(transferOwnership).not.toHaveBeenCalled();
  });

  it.each(["admin", "owner"])("preserves managed write bypass for a system admin with displayed role %s", async (role) => {
    getVaultInfoMock.mockResolvedValue({ role, role_source: "system_admin", managed_by: "collector" });
    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByRole("button", { name: "Invite member" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Change role for mina" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "More actions for mina" }));
    expect(screen.getByRole("menuitem", { name: "Revoke access" })).not.toHaveAttribute("aria-disabled", "true");
    if (role === "owner") {
      expect(screen.getByRole("menuitem", { name: "Transfer ownership" })).not.toHaveAttribute("aria-disabled", "true");
    }
  });

  it("keeps archive restrictions for system admins while allowing ownership transfer", async () => {
    getVaultInfoMock.mockResolvedValue({ role: "owner", role_source: "system_admin", managed_by: "collector", is_archived: true });
    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByRole("button", { name: "Invite member" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "More actions for mina" }));
    expect(screen.getByRole("menuitem", { name: "Revoke access" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("menuitem", { name: "Transfer ownership" })).not.toHaveAttribute("aria-disabled", "true");
  });

  it("describes managed ownership denial independently of archive membership denial", async () => {
    getVaultInfoMock.mockResolvedValue({ role: "owner", role_source: "member", managed_by: "collector", is_archived: true });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "More actions for mina" }));
    expect(screen.getByRole("menuitem", { name: "Revoke access" })).toHaveAccessibleDescription(
      "Vault 'platform-docs' is archived (read-only). Restore the Vault to invite members, change roles, or revoke membership.",
    );
    expect(screen.getByRole("menuitem", { name: "Transfer ownership" })).toHaveAccessibleDescription(
      "Vault 'platform-docs' is write-managed by collector; writes require a granted service token with the required write action",
    );
  });
});


describe("Members loading recovery", () => {
  it("keeps the roster when permissions fail and exposes a busy retry", async () => {
    getVaultInfoMock.mockRejectedValueOnce(new Error("Permission service unavailable"));
    renderPage();
    await screen.findByText("Could not verify Vault permissions");
    expect(screen.getByRole("table", { name: "Vault members" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Invite member" })).toBeNull();
    let resolve!: (value: unknown) => void;
    getVaultInfoMock.mockReturnValueOnce(new Promise(r => { resolve = r; }));
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Retry permissions" }));
    expect(screen.getByTestId("members-workspace-frame")).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("table", { name: "Vault members" })).toBeVisible();
    await act(async () => resolve({ role: "owner" }));
    expect(screen.getByRole("button", { name: "Invite member" })).toBeEnabled();
    expect(screen.getByTestId("members-workspace-frame")).toHaveAttribute("aria-busy", "false");
  });

  it("keeps surviving members and reports revoke success when the following read fails", async () => {
    vi.mocked(revokeAccess).mockResolvedValue({} as never);
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "More actions for mina" }));
    await user.click(screen.getByRole("menuitem", { name: "Revoke access" }));
    getVaultMembersMock.mockRejectedValueOnce(new Error("Roster unavailable"));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Revoke access" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByText("Access revoked for mina.")).toBeVisible();
    expect(screen.getByText("Dana Lee")).toBeVisible();
    expect(screen.queryByText("Mina Park")).toBeNull();
    expect(screen.getByText("Could not update members")).toBeVisible();
    expect(screen.getByTestId("members-workspace-frame")).toHaveAttribute("aria-busy", "false");
  });
});
